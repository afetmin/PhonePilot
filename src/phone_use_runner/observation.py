import hashlib
import json

from .schemas import ElementSelector


def elements(observation: dict) -> list[dict]:
    return (observation.get("tree") or {}).get("elements", [])


def matches(node: dict, selector: ElementSelector) -> bool:
    for field in ("text", "description", "view_id", "editable", "enabled"):
        value = getattr(selector, field)
        if value is not None:
            if node.get(f"{field}_truncated"):
                return False
            default = field == "enabled"
            if node.get(field, default if field in ("enabled", "editable") else None) != value:
                return False
    return selector.action is None or selector.action in node.get("actions", [])


def matching(observation: dict, selector: ElementSelector) -> list[dict]:
    return [node for node in elements(observation) if matches(node, selector)]


def selector_satisfied(observation: dict, selector: ElementSelector) -> bool:
    tree = observation.get("tree") or {}
    if not tree.get("available"):
        return False
    found = matching(observation, selector)
    if selector.match == "exists":
        return bool(found)
    return len(found) == 1 and tree.get("collection_complete") is True


def readable(observation: dict) -> bool:
    return bool((observation.get("tree") or {}).get("available") and elements(observation))


def fingerprint(observation: dict) -> str:
    state = [{key: node.get(key) for key in (
        "text", "description", "label", "hint", "editable", "checked", "selected", "actions"
    )} for node in elements(observation)]
    value = [observation.get("package_name"), state]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_observation(observation: dict | None) -> dict | None:
    if observation is None:
        return None
    result = dict(observation)
    if result.get("screenshot"):
        result["screenshot"] = {k: v for k, v in result["screenshot"].items() if k != "data_base64"}
    result.pop("last_observation", None)
    return result


def collect_text(observation: dict) -> tuple[list[dict], bool]:
    output, seen = [], set()
    truncated = bool((observation.get("tree") or {}).get("collection_truncated"))
    remaining = 12000
    for node in elements(observation):
        if node.get("password"):
            continue
        for field in ("text", "description"):
            value = node.get(field)
            if not isinstance(value, str) or not value.strip() or value in seen:
                continue
            seen.add(value)
            if remaining <= 0:
                truncated = True
                break
            output.append({"element_id": node["element_id"], "field": field, "text": value[:remaining]})
            remaining -= len(value)
            truncated |= bool(node.get(f"{field}_truncated"))
    return output, truncated
