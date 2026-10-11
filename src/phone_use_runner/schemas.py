from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ElementSelector(StrictModel):
    text: str | None = None
    description: str | None = None
    view_id: str | None = None
    editable: bool | None = None
    enabled: bool | None = None
    action: str | None = None
    match: Literal["unique", "exists"] = "unique"

    @model_validator(mode="after")
    def identifier(self):
        if self.text is None and self.description is None and self.view_id is None:
            raise ValueError("A selector needs observed text, description, or view_id")
        return self


class Completion(StrictModel):
    kind: Literal["element", "records", "items", "message", "agent"] = "agent"
    package_name: str | None = None
    selector: ElementSelector | None = None
    min_pages: int = Field(default=1, ge=1, le=100)
    min_items: int = Field(default=1, ge=1, le=100)
    required_fields: list[str] = Field(default_factory=list, max_length=20)
    review_every: int = Field(default=5, ge=1, le=20)
    message: str | None = Field(default=None, max_length=10000)
    recipient: ElementSelector | None = None
    own_message: ElementSelector | None = None

    @model_validator(mode="after")
    def requirements(self):
        if self.kind == "items" and (not self.required_fields or any(not key.strip() for key in self.required_fields)):
            raise ValueError("items completion needs explicit required_fields")
        if self.kind == "element" and self.selector is None:
            raise ValueError("element completion needs a selector")
        if self.kind == "message" and (not self.message or self.recipient is None):
            raise ValueError("message completion needs exact message and recipient selector")
        if self.kind == "message" and self.recipient.match != "unique":
            raise ValueError("Message recipient must have a unique observed header")
        return self


class Subgoal(StrictModel):
    goal: str = Field(min_length=1, max_length=8000)
    app: str | None = None
    input_text: str | None = Field(default=None, max_length=10000)
    completion: Completion = Field(default_factory=Completion)
    collect: bool = False
    commit_selector: ElementSelector | None = None
    context_selector: ElementSelector | None = None

    @model_validator(mode="after")
    def message_guard(self):
        if self.completion.kind == "items" and not self.collect:
            raise ValueError("items completion needs collect=true")
        if self.completion.kind == "message":
            if self.commit_selector is None:
                raise ValueError("A message subgoal needs an explicit send-button commit_selector")
            if self.commit_selector.match != "unique":
                raise ValueError("Message sending needs one unique commit target")
            if self.input_text != self.completion.message:
                raise ValueError("input_text must equal the exact message to send")
        return self


class TaskOptions(StrictModel):
    max_actions: int = Field(default=120, ge=1, le=500)
    max_seconds: int = Field(default=300, ge=1, le=3600)
    max_decision_retries: int = Field(default=3, ge=1, le=20)
    max_no_progress: int = Field(default=3, ge=1, le=20)
    wait_timeout_seconds: float = Field(default=3, ge=0.1, le=30)
    confidence: float = Field(default=0, ge=0, le=1,
                              description="Legacy option accepted for compatibility; decisions are not threshold-gated.")


class RunRequest(StrictModel):
    device_id: str = Field(min_length=1)
    goal: str = Field(min_length=1, max_length=8000)
    subgoals: list[Subgoal] = Field(min_length=1, max_length=30)
    options: TaskOptions = Field(default_factory=TaskOptions)


class VisualAction(StrictModel):
    observation_id: str
    tool: Literal["tap", "swipe"] = "tap"
    points: list[tuple[float, float]] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def shape(self):
        if self.tool == "tap" and len(self.points) != 1:
            raise ValueError("tap needs one image-pixel point")
        if self.tool == "swipe" and len(self.points) < 2:
            raise ValueError("swipe needs at least two image-pixel points")
        return self


class Verification(StrictModel):
    observation_id: str
    completed: bool
    evidence: str = Field(min_length=1, max_length=4000)


class RecordField(StrictModel):
    value: str = Field(min_length=1, max_length=4000)
    record_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=12000)


class RecordItem(StrictModel):
    key: str = Field(min_length=1, max_length=500)
    fields: dict[str, RecordField] = Field(min_length=1, max_length=20)


class RecordReview(StrictModel):
    record_ids: list[str] = Field(min_length=1, max_length=20)
    items: list[RecordItem] = Field(default_factory=list, max_length=100)
    guidance: str | None = Field(default=None, max_length=2000)


class ResumeRequest(StrictModel):
    task_id: str
    subgoal: Subgoal | None = None
    input_text: str | None = Field(default=None, max_length=10000)
    visual_action: VisualAction | None = None
    verification: Verification | None = None
    record_review: RecordReview | None = None

    @model_validator(mode="after")
    def one_change(self):
        if sum(value is not None for value in (
            self.subgoal, self.input_text, self.visual_action, self.verification, self.record_review
        )) != 1:
            raise ValueError("Resume with exactly one subgoal, input_text, visual_action, verification, or record_review")
        return self


class WaitRequest(StrictModel):
    task_id: str
    timeout_seconds: float = Field(default=20, ge=0, le=20)


class CancelRequest(StrictModel):
    task_id: str
