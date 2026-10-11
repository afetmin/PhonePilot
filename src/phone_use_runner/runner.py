import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .collection import apply_review, normalize_text
from .jev import DecisionError, JevPolicy
from .observation import (
    collect_text,
    elements,
    fingerprint,
    public_observation,
    readable,
    selector_satisfied,
)
from .schemas import ResumeRequest, RunRequest
from .settings import Settings
from .transport import BridgeError, PhoneTransport


class YieldTask(Exception):
    pass


@dataclass
class Task:
    id: str
    request: RunRequest
    started: float = field(default_factory=time.monotonic)
    status: str = "running"
    index: int = 0
    actions: int = 0
    observation: dict | None = None
    context: dict | None = None
    pending: dict | None = None
    history: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    pages: set[str] = field(default_factory=set)
    step_pages: set[str] = field(default_factory=set)
    read_states: set[str] = field(default_factory=set)
    items: list[dict] = field(default_factory=list)
    reviewed_records: set[str] = field(default_factory=set)
    content_seen: set[str] = field(default_factory=set)
    states: deque = field(default_factory=lambda: deque(maxlen=12))
    actions_without_content: int = 0
    review_guidance: str | None = None
    retry_context: dict | None = None
    settle_required: bool = False
    completed: list[dict] = field(default_factory=list)
    timings: list[dict] = field(default_factory=list)
    replans: dict[int, int] = field(default_factory=dict)
    commit_attempted: bool = False
    commit_confirmed: bool = False
    commit_baseline: int = 0
    uncertain: bool = False
    stale_failures: int = 0
    confidence_failures: int = 0
    no_progress: int = 0
    decision_retries: int = 0
    consecutive_decision_retries: int = 0
    last_decision: dict | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def step(self):
        return self.request.subgoals[self.index]


class PhoneRunner:
    def __init__(self, settings: Settings | None = None, *, transport=None, policy=None):
        self.settings = settings or Settings.load()
        self.transport = transport or PhoneTransport(self.settings.config)
        self.policy = policy or JevPolicy(self.settings.api_key, self.settings.model)
        self.tasks: dict[str, Task] = {}
        self.devices_in_use: dict[str, str] = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="phone-use")
        self.closed = False
        self.settings.runs.mkdir(parents=True, exist_ok=True, mode=0o700)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            self.closed = True
            tasks = list(self.tasks.values())
        for task in tasks:
            if task.status in {"running", "needs_context"}:
                task.cancelled.set()
                task.status = "cancelled"
                task.context = {"reason": "runner_closed", "request": task.pending}
                self._publish(task, "runner_closed")
        self.pool.shutdown(wait=True, cancel_futures=True)
        close = getattr(self.policy, "close", None)
        if close:
            close()
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def devices(self):
        result = self.transport.devices()
        for device in result.get("devices", []):
            if device.get("online"):
                try:
                    device["capabilities"] = self.transport.call(device["device_id"], "get_capabilities", {})
                    device["apps"] = self.transport.call(device["device_id"], "list_apps", {})
                except Exception as error:
                    device["discovery_error"] = type(error).__name__
                    if isinstance(error, BridgeError):
                        device["discovery_error_code"] = error.value["error"]["code"]
        return result

    def run(self, request: RunRequest | dict) -> dict:
        request = RunRequest.model_validate(request).model_copy(deep=True)
        if not getattr(self.policy, "api_key", True):
            raise ValueError("Configure TYPESAFE_API_KEY before running a real task")
        with self.lock:
            if self.closed:
                raise ValueError("Runner is closed")
            if request.device_id in self.devices_in_use:
                raise ValueError("This device already has a task; finish or cancel it first")
            task = Task(str(uuid.uuid4()), request)
            self.tasks[task.id] = task
            self.devices_in_use[request.device_id] = task.id
            self._publish(task, "task_started")
            result = self._result(task)
            self.pool.submit(self._work, task)
            return result

    def _task(self, task_id: str) -> Task:
        with self.lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise ValueError("Task is not in this process; inspect its saved report before starting a replacement")
        return task

    def wait(self, task_id: str, timeout_seconds: float = 20) -> dict:
        if not 0 <= timeout_seconds <= 20:
            raise ValueError("timeout_seconds must be between 0 and 20")
        task = self._task(task_id)
        end = time.monotonic() + timeout_seconds
        with task.condition:
            while task.status == "running" and time.monotonic() < end:
                task.condition.wait(end - time.monotonic())
            return self._result(task)

    def screenshot(self, task_id: str) -> dict | None:
        task = self._task(task_id)
        with task.condition:
            if task.status != "needs_context" or not task.observation:
                return None
            screenshot = task.observation.get("screenshot")
            return dict(screenshot) if screenshot and screenshot.get("available") else None

    def resume(self, request: ResumeRequest | dict) -> dict:
        request = ResumeRequest.model_validate(request)
        task = self._task(request.task_id)
        with task.condition:
            if task.status != "needs_context":
                raise ValueError("Only needs_context tasks can resume")
            if task.cancelled.is_set():
                raise ValueError("Task has been cancelled")
            if time.monotonic() - task.started >= task.request.options.max_seconds:
                task.status = "blocked"
                task.context = {"reason": "time_budget_exhausted", "request": task.pending}
                self._publish(task, "blocked")
                return self._result(task)
            reason = (task.context or {}).get("reason")
            if task.uncertain and request.verification is None:
                raise ValueError("Resolve the original action outcome with current evidence before resuming")
            if request.record_review:
                if reason != "records_review" or task.step.completion.kind != "items":
                    raise ValueError("record_review requires an items collection review handoff")
                pending_ids = task.context.get("record_ids", [])
                task.items = apply_review(task.records, task.items, task.reviewed_records, request.record_review,
                                          subgoal=task.index, required_fields=task.step.completion.required_fields,
                                          pending_ids=pending_ids)
                task.reviewed_records.update(pending_ids)
                task.review_guidance = request.record_review.guidance
                task.actions_without_content = 0
                task.states.clear()
                if self._completion_issue(task) is None:
                    self._advance(task, {"kind": "reviewed_items", "count": len(self._step_items(task))})
            elif request.verification:
                verification = request.verification
                if not task.observation or verification.observation_id != task.observation.get("observation_id"):
                    raise ValueError("Verification must reference the current supplied observation")
                if reason not in {"verification_required", "message_verification_required", "action_outcome_unknown"}:
                    raise ValueError("This handoff requires a plan, input, or visual action, not completion verification")
                if task.uncertain and not verification.completed:
                    task.status = "blocked"
                    task.context = {"reason": "action_outcome_unresolved", "evidence": verification.evidence,
                                    "request": task.pending, "message": "Input may have executed; no replacement sent"}
                    self._publish(task, "blocked")
                    return self._result(task)
                if verification.completed:
                    task.uncertain = False
                    task.pending = None
                    self._advance(task, {"kind": "agent", **verification.model_dump()})
                else:
                    self._count_replan(task)
            elif request.visual_action:
                if task.step.completion.kind == "message":
                    raise ValueError("Message sending needs validated nodes; use a separate navigation subgoal for visual focus")
                if not task.observation or request.visual_action.observation_id != task.observation.get("observation_id"):
                    raise ValueError("Visual action must reference the supplied screenshot observation")
                if task.commit_attempted:
                    raise ValueError("A send has already been attempted; verify its result instead of tapping again")
                shot = task.observation.get("screenshot") or {}
                if not shot.get("available"):
                    raise ValueError("No usable screenshot was supplied")
                task.context = {"visual_action": request.visual_action.model_dump()}
            else:
                self._count_replan(task)
                if request.subgoal:
                    if task.commit_attempted:
                        raise ValueError("A send has been attempted; verify the original subgoal first")
                    task.request.subgoals[task.index] = request.subgoal
                    task.step_pages.clear()
                    task.states.clear()
                    task.actions_without_content = 0
                else:
                    updated = task.step.model_dump()
                    updated["input_text"] = request.input_text
                    task.request.subgoals[task.index] = type(task.step).model_validate(updated)
            if task.status == "completed":
                self._publish(task, "completed")
                return self._result(task)
            if task.status == "blocked":
                self._publish(task, "blocked")
                return self._result(task)
            task.status = "running"
            if not request.visual_action:
                task.context = None
                task.observation = None
            task.no_progress = task.stale_failures = task.confidence_failures = 0
            task.consecutive_decision_retries = 0
            self._publish(task, "resumed")
            self.pool.submit(self._work, task)
            return self._result(task)

    def _count_replan(self, task: Task):
        count = task.replans.get(task.index, 0) + 1
        task.replans[task.index] = count
        if count > 2:
            task.status = "blocked"
            task.context = {"reason": "replan_budget_exhausted"}

    def cancel(self, task_id: str) -> dict:
        task = self._task(task_id)
        task.cancelled.set()
        with task.condition:
            if task.status in {"running", "needs_context"}:
                task.status = "cancelled"
                task.context = {"reason": "cancelled", "request": task.pending,
                                "message": "Future actions stopped; an already dispatched input may complete"}
                self._publish(task, "cancelled")
        # Cancellation is scoped to this task's original request, not all client input.
        if task.pending and not self.closed:
            self.pool.submit(self._cancel_pending, task.request.device_id, task.pending["request_id"])
        return self.wait(task.id, 0)

    def _cancel_pending(self, device_id, request_id):
        try:
            self.transport.call(device_id, "cancel", {"request_id": request_id})
        except Exception:
            pass

    def _check(self, task: Task):
        if task.cancelled.is_set() or self.closed:
            task.status = "cancelled"
            raise YieldTask()
        if time.monotonic() - task.started >= task.request.options.max_seconds:
            task.status = "blocked"
            task.context = {"reason": "time_budget_exhausted", "request": task.pending}
            raise YieldTask()

    def _call(self, task: Task, tool: str, arguments: dict) -> dict:
        started = time.perf_counter()
        try:
            return self.transport.call(task.request.device_id, tool, arguments)
        finally:
            task.timings.append({"stage": "phone_rpc", "tool": tool,
                                 "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})

    def _observe(self, task: Task, *, mode="tree", details=False) -> dict:
        self._check(task)
        details = details or self._needs_details(task)
        result = self._call(task, "observe", {"mode": mode, "output": "decision", "compact": True,
                                              "include_details": details, "stable_wait_ms": 0, "timeout_ms": 3000})
        if result.get("error") or result.get("observation_status") == "failed":
            task.status = "blocked"
            task.context = {"reason": "observation_failed", "error": result.get("error")}
            raise YieldTask()
        task.observation = result
        task.timings.append({"stage": "phone_collection", "mode": mode,
                             "collection_ms": result.get("collection_ms"), "wait_ms": result.get("wait_ms"),
                             "output_bytes": (result.get("tree") or {}).get("output_bytes")})
        return result

    @staticmethod
    def _needs_details(task):
        selectors = [task.step.completion.selector, task.step.completion.recipient,
                     task.step.completion.own_message, task.step.commit_selector, task.step.context_selector]
        return any(selector and selector.view_id for selector in selectors)

    def _handoff(self, task: Task, reason: str, *, image=False, **details):
        if task.cancelled.is_set():
            raise YieldTask()
        if image:
            try:
                self._observe(task, mode="both", details=True)
            except YieldTask:
                raise
            shot = (task.observation or {}).get("screenshot") or {}
            if not shot.get("available"):
                task.status = "blocked"
                task.context = {"reason": "screenshot_unavailable", "original_reason": reason,
                                "screenshot": shot, **details}
                raise YieldTask()
            if shot.get("data_base64"):
                path = self.settings.runs / task.id / "handoff.jpg"
                path.write_bytes(base64.b64decode(shot["data_base64"], validate=True))
                os.chmod(path, 0o600)
                shot["saved_path"] = str(path)
        task.status = "needs_context"
        task.context = {"reason": reason, **details}
        raise YieldTask()

    def _advance(self, task: Task, evidence: dict):
        task.completed.append({"subgoal": task.index, "goal": task.step.goal, "evidence": evidence})
        task.index += 1
        task.step_pages.clear()
        task.read_states.clear()
        task.content_seen.clear()
        task.states.clear()
        task.actions_without_content = 0
        task.review_guidance = task.retry_context = None
        task.settle_required = False
        task.commit_attempted = task.commit_confirmed = False
        task.commit_baseline = 0
        task.no_progress = task.stale_failures = task.confidence_failures = 0
        task.consecutive_decision_retries = 0
        task.last_decision = None
        task.observation = None
        if task.index == len(task.request.subgoals):
            task.status = "completed"
            task.context = None

    def _collect(self, task: Task):
        before = task.observation or {}
        original_state = fingerprint(before)
        observation = self._observe(task, details=True)
        expected_app = task.step.app or task.step.completion.package_name
        if expected_app and observation.get("package_name") != expected_app:
            self._retry_decision(task, reason="collection_package_mismatch", expected=expected_app)
            return
        source, truncated = collect_text(observation)
        if not source:
            self._handoff(task, "source_text_unavailable", image=True)
        page = hashlib.sha256(json.dumps([observation.get("package_name"),
                                          [item["text"] for item in source]], ensure_ascii=False).encode()).hexdigest()
        if page not in task.pages or (task.step.completion.kind == "items" and page not in task.step_pages):
            task.records.append({"id": str(uuid.uuid4()), "page_id": page,
                                 "package_name": observation.get("package_name"),
                                 "subgoal": task.index, "observation_id": observation.get("observation_id"),
                                 "captured_at_ms": observation.get("captured_at_ms"),
                                 "source": source, "truncated": truncated})
            task.pages.add(page)
        task.step_pages.add(page)
        task.read_states.update([original_state, fingerprint(observation)])
        task.content_seen.update(normalize_text(item["text"]) for item in source if len(item["text"].strip()) >= 32)

    @staticmethod
    def _step_items(task: Task) -> list[dict]:
        return [item for item in task.items if item["subgoal"] == task.index]

    def _review_batch(self, task: Task, *, force=False):
        if task.step.completion.kind != "items":
            return
        pending = [record for record in task.records
                   if record["subgoal"] == task.index and record["id"] not in task.reviewed_records]
        remaining = max(1, task.step.completion.min_items - len(self._step_items(task)))
        size = min(task.step.completion.review_every, remaining)
        if pending and (force or len(pending) >= size):
            batch = pending[:size]
            self._handoff(task, "records_review", record_ids=[record["id"] for record in batch],
                          records=batch, required_fields=task.step.completion.required_fields,
                          accepted_item_count=len(self._step_items(task)), target=task.step.completion.min_items,
                          message="Review this batch against the goal, merge duplicate items, and return source-backed fields")

    def _commit_ready(self, task: Task) -> bool:
        return not task.commit_attempted and task.step.commit_selector is not None

    def _completion_issue(self, task: Task) -> dict | None:
        completion = task.step.completion
        observation = task.observation or {}
        if task.step.app and observation.get("package_name") != task.step.app:
            return {"reason": "app_mismatch", "expected": task.step.app,
                    "actual": observation.get("package_name")}
        if completion.package_name is not None and observation.get("package_name") != completion.package_name:
            return {"reason": "package_mismatch", "expected": completion.package_name,
                    "actual": observation.get("package_name")}
        if completion.kind == "element":
            if completion.selector is None or not selector_satisfied(observation, completion.selector):
                return {"reason": "element_not_satisfied"}
        elif completion.kind == "records":
            pages = len(task.step_pages)
            if pages < completion.min_pages:
                return {"reason": "insufficient_pages", "pages": pages,
                        "required": completion.min_pages}
        elif completion.kind == "items":
            count = len(self._step_items(task))
            if count < completion.min_items:
                return {"reason": "insufficient_items", "items": count, "required": completion.min_items}
        elif completion.kind == "message" and not task.commit_confirmed:
            return {"reason": "message_not_committed"}
        return None

    def _reconcile(self, task: Task, request_id: str) -> dict | None:
        end = min(task.started + task.request.options.max_seconds, time.monotonic() + 2)
        while time.monotonic() < end and not task.cancelled.is_set():
            try:
                value = self._call(task, "get_status", {"request_id": request_id})
                result = value.get("request")
                if isinstance(result, dict) and result.get("state") not in {"accepted", "executing"}:
                    return result
                if value.get("error"):
                    return None
            except Exception:
                return None
            task.cancelled.wait(0.1)
        return None

    def _act(self, task: Task, tool: str, arguments: dict, *, commit=False, target_evidence=None) -> bool:
        self._check(task)
        if task.actions >= task.request.options.max_actions:
            task.status = "blocked"
            task.context = {"reason": "action_budget_exhausted"}
            raise YieldTask()
        request_id = f"{task.id}:{task.actions + 1}"
        args = {**arguments, "request_id": request_id, "observe_after": True,
                "observation_options": {"mode": "tree", "output": "decision", "compact": True,
                                        "include_details": self._needs_details(task),
                                        "timeout_ms": 3000, "stable_wait_ms": 0}}
        if task.observation:
            args["service_instance_id"] = task.observation["service_instance_id"]
            if tool not in {"launch_app", "back", "home", "recents"}:
                args["observation_id"] = task.observation["observation_id"]
                args["expected_window_id"] = task.observation["window_id"]
        if commit:
            task.commit_baseline = sum(not n.get("editable") and n.get("text") == task.step.completion.message
                                       for n in elements(task.observation or {}))
            task.commit_attempted = True
        task.actions += 1
        task.pending = {"request_id": request_id, "tool": tool, "subgoal": task.index, "commit": commit}
        # Flush intent before dispatch. After a process restart, reports cannot be treated as unsent work.
        self._publish(task, "action_intent")
        self._check_after_dispatch(task)
        started = time.perf_counter()
        try:
            result = self._call(task, tool, args)
        except Exception as error:
            value = getattr(error, "value", {})
            if value.get("action_executed") is False:
                result = {**value, "state": "failed"}
            else:
                result = self._reconcile(task, request_id)
        if isinstance(result, dict) and result.get("state") in {"accepted", "executing", "unknown"}:
            result = self._reconcile(task, request_id)
        if result is None or result.get("action_executed") is None:
            task.uncertain = True
            self._handoff(task, "action_outcome_unknown", image=True, request=task.pending,
                          message="Resolve the original request with current evidence; no replacement action was sent")
        task.pending = None
        task.history.append({"tool": tool, "element_id": arguments.get("element_id"),
                             "direction": arguments.get("direction"),
                             "commit": commit,
                             "target": target_evidence,
                             "subgoal": task.index, "action_executed": result.get("action_executed"),
                             "error": result.get("error")})
        task.timings.append({"stage": "action", "tool": tool,
                             "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
        self._check_after_dispatch(task)
        if result.get("action_executed") is False:
            if commit:
                task.commit_attempted = False
            code = (result.get("error") or {}).get("code")
            if code in {"STALE_ELEMENT", "STALE_OBSERVATION", "WINDOW_MISMATCH", "ELEMENT_UNAVAILABLE"}:
                task.stale_failures += 1
            else:
                task.stale_failures = 0
            task.observation = None
            self._publish(task, "action_failed")
            return False
        task.stale_failures = 0
        if commit:
            task.commit_confirmed = True
        observation = result.get("observation")
        if observation and observation.get("observation_status") != "failed" and not observation.get("error"):
            task.observation = observation
        else:
            task.observation = None
        task.settle_required = tool in {"launch_app", "tap", "back"}
        self._publish(task, "action_executed")
        return True

    def _check_after_dispatch(self, task):
        if task.cancelled.is_set() or self.closed:
            task.status = "cancelled"
            raise YieldTask()

    def _visual(self, task: Task):
        visual = (task.context or {}).get("visual_action")
        if not visual:
            return
        shot = (task.observation or {}).get("screenshot") or {}
        points = []
        for x, y in visual["points"]:
            if not 0 <= x < shot["width"] or not 0 <= y < shot["height"]:
                raise ValueError("Visual point is outside the supplied screenshot")
            points.append({"x": x * shot["scale_x"] + shot.get("offset_x", 0),
                           "y": y * shot["scale_y"] + shot.get("offset_y", 0)})
        task.context = None
        args = points[0] if visual["tool"] == "tap" else {"points": points, "duration_ms": 300}
        if not self._act(task, visual["tool"], args):
            self._retry_decision(task, reason="action_failed")

    def _decision_state(self, task: Task) -> dict:
        completion_issue = self._completion_issue(task)
        state = {"goal": task.request.goal, "subgoal_index": task.index,
                 "completed_subgoals": [{"subgoal": item["subgoal"], "goal": item["goal"][:200]}
                                       for item in task.completed[-3:]],
                 "commit_attempted": task.commit_attempted, "commit_confirmed": task.commit_confirmed,
                 "action_outcome_unknown": task.uncertain, "decision_retries": task.decision_retries,
                 "consecutive_decision_retries": task.consecutive_decision_retries,
                 "completion_ready": completion_issue is None, "completion_issue": completion_issue,
                 "collected_pages": len(task.step_pages),
                 "previous_decision": task.last_decision}
        if task.retry_context:
            state["retry_context"] = task.retry_context
        if task.step.collect:
            state["actions_without_new_content"] = task.actions_without_content
        if task.step.completion.kind == "items":
            items = self._step_items(task)
            state["accepted_item_count"] = len(items)
            state["target_item_count"] = task.step.completion.min_items
            state["recent_items"] = [{"key": item["key"],
                                      "fields": {key: value["value"][:160] for key, value in item["fields"].items()}}
                                     for item in items[-10:]]
            state["review_guidance"] = task.review_guidance
        if task.step.completion.kind == "message":
            nodes = elements(task.observation or {})
            count = sum(not n.get("editable") and n.get("text") == task.step.completion.message for n in nodes)
            editors = [n for n in nodes if n.get("editable") and not n.get("password")]
            state["message_evidence"] = {"visible_message_count": count,
                                         "count_before_commit": task.commit_baseline,
                                         "new_message_visible": task.commit_confirmed and count > task.commit_baseline,
                                         "composer_cleared": bool(editors) and all(not n.get("text") for n in editors),
                                         "draft_matches_input": any(n.get("text") == task.step.input_text for n in editors)}
        return state

    def _retry_decision(self, task: Task, reason: str = "decision_unresolved", **details):
        # A missing target or transient tree stays inside the worker until its retry budget is exhausted.
        task.decision_retries += 1
        task.consecutive_decision_retries += 1
        task.retry_context = {"reason": reason, **details}
        self._publish(task, "decision_retry")
        retry_details = {**details, "retry_count": task.decision_retries,
                         "consecutive_retry_count": task.consecutive_decision_retries,
                         "last_decision": task.last_decision}
        max_retries = task.request.options.max_decision_retries
        if task.consecutive_decision_retries >= max_retries:
            self._check(task)
            self._review_batch(task, force=True)
            self._handoff(task, reason, image=False, **retry_details)
        task.cancelled.wait(0.25)
        self._check(task)
        self._observe(task, details=True)

    def _settle(self, task: Task):
        # The bridge does not implement stable_wait_ms. Debounce its actual UI events instead.
        task.settle_required = False
        if task.observation is None:
            self._observe(task)
        end = min(time.monotonic() + 0.4, task.started + task.request.options.max_seconds)
        changed = False
        while time.monotonic() < end:
            self._check(task)
            result = self._call(task, "wait_for_change", {
                "ui_version": task.observation["ui_version"],
                "timeout_ms": max(1, min(100, int((end - time.monotonic()) * 1000))),
            })
            if result.get("error"):
                break
            if not result.get("changed") and result.get("ui_version", task.observation["ui_version"]) == task.observation["ui_version"]:
                break
            self._observe(task)
            changed = True
        if changed:
            self._publish(task, "ui_settled")

    def _record_progress(self, task: Task, before: str, before_pages: int, before_content: int = 0):
        if task.observation is None:
            self._observe(task)
        changed = fingerprint(task.observation) != before
        new_page = len(task.step_pages) > before_pages
        new_content = len(task.content_seen) > before_content
        state = fingerprint(task.observation)
        if new_content:
            task.states.clear()
            task.actions_without_content = 0
        elif task.step.collect:
            task.actions_without_content += 1
        task.states.append(state)
        if changed or new_page:
            task.no_progress = 0
            task.consecutive_decision_retries = 0
            task.retry_context = None
        else:
            task.no_progress += 1
        if task.no_progress >= task.request.options.max_no_progress:
            self._check(task)
            self._review_batch(task, force=True)
            self._handoff(task, "no_progress", image=False)
        if task.states.count(state) > task.request.options.max_no_progress:
            self._review_batch(task, force=True)
            self._handoff(task, "navigation_loop", image=False,
                          repeated_state_count=task.states.count(state))
        if task.step.collect and task.actions_without_content >= max(12, task.request.options.max_no_progress * 4):
            self._review_batch(task, force=True)
            self._handoff(task, "no_new_content", image=False,
                          actions_without_new_content=task.actions_without_content)

    def _work(self, task: Task):
        try:
            self._visual(task)
            while task.status == "running":
                self._check(task)
                if task.observation is None:
                    self._observe(task)
                if task.settle_required:
                    self._settle(task)
                self._review_batch(task)
                auto_complete = (task.step.completion.kind == "items"
                                 or (task.step.completion.kind == "element" and not task.step.collect))
                if auto_complete and self._completion_issue(task) is None:
                    self._advance(task, {"kind": "validated_completion"})
                    self._publish(task, "subgoal_completed")
                    continue
                if task.step.app and task.observation.get("package_name") != task.step.app:
                    before = fingerprint(task.observation)
                    before_pages = len(task.step_pages)
                    if not self._act(task, "launch_app", {"package_name": task.step.app}):
                        self._retry_decision(task, reason="action_failed")
                        continue
                    self._record_progress(task, before, before_pages, len(task.content_seen))
                    self._publish(task, "progress")
                    continue
                if not readable(task.observation):
                    self._retry_decision(task)
                    continue
                before = fingerprint(task.observation)
                before_pages = len(task.step_pages)
                before_content = len(task.content_seen)
                cycle_started = time.perf_counter()
                decision = self.policy.choose(task.observation, task.step, task.history,
                                              commit_available=self._commit_ready(task),
                                              can_read=task.step.collect and before not in task.read_states,
                                              task_state=self._decision_state(task))
                self._check(task)
                task.timings.append({"stage": "jev", "elapsed_ms": decision.latency_ms,
                                     "model": decision.model, "confidence": decision.confidence,
                                     "usage": decision.usage})
                task.confidence_failures = 0
                operation = decision.operation
                target = decision.target
                task.last_decision = {"operation": operation,
                                      "requested_operation": decision.answers.get("operation", {}).get("choice", operation),
                                      "target_element_id": target.get("element_id") if target else None,
                                      "confidence": decision.confidence,
                                      "observation_id": task.observation.get("observation_id")}
                if operation in {"NEEDS_CONTEXT", "BLOCKED"}:
                    self._retry_decision(task)
                    continue
                elif operation == "DONE":
                    issue = self._completion_issue(task)
                    if issue is not None:
                        self._review_batch(task, force=True)
                        self._retry_decision(task, reason="completion_not_satisfied", issue=issue)
                        continue
                    self._advance(task, {"kind": "agent", "operation": operation,
                                         "observation_id": task.observation.get("observation_id"),
                                         "confidence": decision.confidence})
                    self._publish(task, "subgoal_completed")
                    continue
                elif operation == "READ":
                    self._collect(task)
                elif operation == "WAIT":
                    wait_version = task.observation["ui_version"]
                    wait_started = time.perf_counter()
                    wait_deadline = min(
                        time.monotonic() + task.request.options.wait_timeout_seconds,
                        task.started + task.request.options.max_seconds,
                    )
                    changed = False
                    timed_out = False
                    try:
                        while task.status == "running":
                            self._check(task)
                            remaining = wait_deadline - time.monotonic()
                            if remaining <= 0:
                                timed_out = True
                                break
                            timeout_ms = min(500, int(remaining * 1000))
                            if timeout_ms <= 0:
                                timed_out = True
                                break
                            waited = self._call(task, "wait_for_change", {
                                "ui_version": wait_version, "timeout_ms": timeout_ms,
                            })
                            if waited.get("changed") or (
                                waited.get("ui_version") is not None and waited.get("ui_version") != wait_version
                            ):
                                task.observation = None
                                task.no_progress = 0
                                task.consecutive_decision_retries = 0
                                changed = True
                                break
                        if not changed and task.status == "running" and not timed_out:
                            timed_out = True
                    finally:
                        task.timings.append({"stage": "wait", "elapsed_ms": round(
                            (time.perf_counter() - wait_started) * 1000, 2)})
                    if timed_out:
                        self._retry_decision(task, reason="wait_timeout")
                        continue
                    self._publish(task, "progress")
                    continue
                elif operation == "BACK":
                    if not self._act(task, "back", {}):
                        self._retry_decision(task, reason="action_failed")
                        continue
                elif target:
                    args = {"element_id": target["element_id"]}
                    if operation == "INPUT_TEXT":
                        if task.step.input_text is None:
                            self._handoff(task, "input_required")
                        args.update(text=task.step.input_text, mode="replace")
                        tool = "input_text"
                    elif operation.startswith("SCROLL_"):
                        tool = "scroll"
                        args["direction"] = "forward" if operation == "SCROLL_FORWARD" else "backward"
                    else:
                        tool = "long_press" if operation == "LONG_PRESS" else "tap"
                    evidence = {key: target[key] for key in ("label", "text", "description", "view_id", "editable")
                                if target.get(key) is not None}
                    if not self._act(task, tool, args, commit=operation == "COMMIT", target_evidence=evidence):
                        self._retry_decision(task, reason="action_failed", error=task.history[-1].get("error"))
                        continue
                else:
                    self._retry_decision(task)
                    continue
                task.timings.append({"stage": "decision_action_cycle", "operation": operation,
                                     "elapsed_ms": round((time.perf_counter() - cycle_started) * 1000, 2)})
                self._record_progress(task, before, before_pages, before_content)
                self._publish(task, "progress")
        except YieldTask:
            pass
        except DecisionError as error:
            task.status = "needs_context"
            task.context = {"reason": "decision_failed", "message": str(error)}
        except BridgeError as error:
            code = error.value["error"]["code"]
            task.status = "blocked"
            task.context = {"reason": "bridge_error", "error_type": "BridgeError", "error": {"code": code},
                            "recovery": "pair_device" if code in {"NOT_PAIRED", "TLS_NOT_PAIRED"} else "check_bridge",
                            "request": task.pending}
        except Exception as error:
            task.status = "blocked"
            # Provider/network exceptions may embed sensitive URLs or headers; expose their type only.
            task.context = {"reason": "runner_error", "error_type": type(error).__name__, "request": task.pending}
        finally:
            with task.condition:
                self._publish(task, "worker_yielded")

    def _result(self, task: Task) -> dict:
        index = task.index
        items_workflow = any(step.completion.kind == "items" for step in task.request.subgoals)
        records = [] if items_workflow else task.records[:20]
        result = {"task_id": task.id, "device_id": task.request.device_id, "goal": task.request.goal,
                  "status": task.status, "subgoal_index": index,
                  "subgoal": task.request.subgoals[index].model_dump(exclude_none=True) if index < len(task.request.subgoals) else None,
                  "actions": task.actions, "elapsed_ms": round((time.monotonic() - task.started) * 1000),
                  "commit_attempted": task.commit_attempted, "commit_confirmed": task.commit_confirmed,
                  "action_outcome_unknown": task.uncertain,
                  "decision_retries": task.decision_retries,
                  "consecutive_decision_retries": task.consecutive_decision_retries,
                  "last_decision": task.last_decision,
                  "context": task.context, "pending_action": task.pending, "completed_subgoals": task.completed,
                  "records": records, "record_count": len(task.records),
                  "records_omitted": len(task.records) - len(records), "recent_actions": task.history[-10:],
                  "items": task.items, "item_count": len(task.items),
                  "timings": task.timings[-30:], "observation": public_observation(task.observation),
                  "report_path": str(self.settings.runs / task.id / "result.json")}
        return result

    def _publish(self, task: Task, event: str):
        with task.condition:
            directory = self.settings.runs / task.id
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = self._result(task)
            result["records"] = task.records
            result["records_omitted"] = 0
            result["timings"] = task.timings
            result["recent_actions"] = task.history
            path = directory / "result.json"
            temporary = directory / "result.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                os.chmod(stream.name, 0o600)
                stream.write(json.dumps({"event": event, "at_ms": int(time.time() * 1000),
                                         "status": task.status, "subgoal": task.index,
                                         "actions": task.actions, "pending": task.pending,
                                         "record_count": len(task.records), "item_count": len(task.items),
                                         "decision": task.last_decision,
                                         "retry": task.retry_context if event == "decision_retry" else None,
                                         "action": task.history[-1] if event in {"action_executed", "action_failed"} and task.history else None,
                                         "context": {key: value for key, value in (task.context or {}).items()
                                                     if key not in {"records", "visual_action"}}}) + "\n")
            if task.status in {"completed", "blocked", "cancelled"} and task.pending is None:
                with self.lock:
                    if self.devices_in_use.get(task.request.device_id) == task.id:
                        self.devices_in_use.pop(task.request.device_id)
            task.condition.notify_all()
