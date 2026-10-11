"""Validation and merging for source-backed collection reviews."""

from __future__ import annotations

import copy
import re
import unicodedata

from .schemas import RecordReview

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Apply NFKC normalization, whitespace folding, and trimming."""

    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def _key(value: str, label: str) -> tuple[str, str]:
    normalized = normalize_text(value)
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return value, normalized.casefold()


def _review_fields(item, records_by_id: dict[str, dict], current_ids: set[str], reviewed_ids: set[str],
                   subgoal: int, label: str) -> tuple[dict, bool]:
    fields: dict = {}
    names: set[str] = set()
    has_current = False
    for name, field in item.fields.items():
        normalized_name = normalize_text(name)
        if not normalized_name:
            raise ValueError(f"{label} has an empty field name")
        if normalized_name in names:
            raise ValueError(f"{label} has duplicate normalized field names")
        names.add(normalized_name)

        record_id = field.record_id
        record = records_by_id.get(record_id)
        if record is None or record.get("subgoal") != subgoal:
            raise ValueError(f"{label}.{name} references an invalid record")
        if record_id not in current_ids and record_id not in reviewed_ids:
            raise ValueError(f"{label}.{name} references an unreviewed record")

        value = normalize_text(field.value)
        quote = normalize_text(field.quote)
        if not value or not quote or value not in quote:
            raise ValueError(f"{label}.{name} has invalid value/quote grounding")
        if not any(quote in normalize_text(source["text"]) for source in record.get("source", [])):
            raise ValueError(f"{label}.{name} quote is not grounded in its record")

        fields[normalized_name] = {
            "value": field.value,
            "record_id": record_id,
            "quote": field.quote,
        }
        has_current = has_current or record_id in current_ids
    return fields, has_current


def apply_review(
    records: list[dict],
    existing_items: list[dict],
    reviewed_ids: set[str],
    review: RecordReview,
    *,
    subgoal: int,
    required_fields: list[str],
    pending_ids: list[str],
) -> list[dict]:
    """Atomically validate and merge one review batch."""

    review_ids = list(review.record_ids)
    pending = list(pending_ids)
    if len(review_ids) != len(set(review_ids)) or set(review_ids) != set(pending):
        raise ValueError("review.record_ids must uniquely match pending_ids")
    current_ids = set(review_ids)

    records_by_id: dict[str, dict] = {}
    for record in records:
        record_id = record["id"]
        if record_id in records_by_id:
            raise ValueError(f"duplicate record id: {record_id}")
        records_by_id[record_id] = record
        if record_id in current_ids and record.get("subgoal") != subgoal:
            raise ValueError(f"record {record_id} belongs to another subgoal")
    for record_id in current_ids:
        if record_id not in records_by_id:
            raise ValueError(f"unknown pending record: {record_id}")

    merged = copy.deepcopy(existing_items)
    current_indexes: dict[str, int] = {}
    for index, item in enumerate(merged):
        if item["subgoal"] != subgoal:
            continue
        display_key, normalized_key = _key(item["key"], f"existing_items[{index}].key")
        if normalized_key in current_indexes:
            first = merged[current_indexes[normalized_key]]
            first["fields"].update(item["fields"])
            merged[index] = None
        else:
            item["key"] = display_key
            current_indexes[normalized_key] = index

    new_indexes: dict[str, int] = {}
    new_has_current: dict[str, bool] = {}
    for item_index, item in enumerate(review.items):
        display_key, normalized_key = _key(item.key, f"review.items[{item_index}].key")
        fields, has_current = _review_fields(
            item, records_by_id, current_ids, reviewed_ids, subgoal, f"review.items[{item_index}]"
        )
        if normalized_key in current_indexes:
            merged[current_indexes[normalized_key]]["fields"].update(fields)
            continue
        if normalized_key in new_indexes:
            merged[new_indexes[normalized_key]]["fields"].update(fields)
            new_has_current[normalized_key] |= has_current
            continue
        new_indexes[normalized_key] = len(merged)
        new_has_current[normalized_key] = has_current
        merged.append({"key": display_key, "subgoal": subgoal, "fields": fields})

    for normalized_key, has_current in new_has_current.items():
        if not has_current:
            raise ValueError(f"new item {normalized_key!r} must cite a pending record")

    required = []
    for name in required_fields:
        normalized_name = normalize_text(name)
        if not normalized_name:
            raise ValueError("required field names must not be empty")
        if normalized_name not in required:
            required.append(normalized_name)
    result = [item for item in merged if item is not None]
    for index, item in enumerate(result):
        if item["subgoal"] != subgoal:
            continue
        names = {normalize_text(name) for name in item["fields"]}
        missing = [name for name in required if name not in names]
        if missing:
            raise ValueError(f"item {index} is missing required fields: {', '.join(missing)}")
    return result
