"""Build and validate the frozen, bounded related-work planning snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MEDIA_TYPE = "application/x-ndjson"
SNAPSHOT_NAME = "related-work.jsonl"
MAX_RECORDS = 64
MAX_BYTES = 256 * 1024
MAX_ANCESTORS = 3
PLANNING_TEXT_FIELDS = (
    "title",
    "status",
    "description",
    "design",
    "acceptance_criteria",
)
REFERENCE_FIELDS = (
    "parent",
    "blockers",
    "dependents",
)
SAFE_FIELDS = (*PLANNING_TEXT_FIELDS, *REFERENCE_FIELDS)
RELATIONSHIP_ORDER = {
    "subject": 0,
    "parent": 1,
    "sibling": 2,
    "blocker": 3,
    "dependent": 4,
    "ancestor": 5,
}
BLOCKING_RELATION_TYPES = {
    "blocks",
    "blocker",
    "blocking",
    "blocked-by",
    "blocked_by",
}


class RelatedWorkError(ValueError):
    pass


def _identifier(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for name in ("id", "issue_id", "dependency_id", "depends_on_id"):
            if isinstance(value.get(name), str) and value[name]:
                return value[name]
    return None


def _items(record, name):
    value = record.get(name, [])
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    return value if isinstance(value, list) else []


def _relation_type(value):
    if not isinstance(value, dict):
        return ""
    return str(value.get("dependency_type") or value.get("type") or "").lower()


def parent_id(record):
    for name in ("parent", "parent_id"):
        found = _identifier(record.get(name))
        if found:
            return found
    for item in _items(record, "dependencies"):
        if _relation_type(item) in {"parent-child", "parent_child", "parent"}:
            return _identifier(item)
    return None


def child_ids(record):
    result = {_identifier(item) for item in _items(record, "children")}
    for item in _items(record, "dependents"):
        if _relation_type(item) in {"parent-child", "parent_child", "child"}:
            result.add(_identifier(item))
    return {item for item in result if item}


def _blocking_item(item, *, dedicated):
    relation = _relation_type(item)
    return relation in BLOCKING_RELATION_TYPES or dedicated and not relation


def blocker_ids(record):
    result = {
        _identifier(item)
        for item in _items(record, "blockers")
        if _blocking_item(item, dedicated=True)
    }
    result.update(
        _identifier(item)
        for item in _items(record, "dependencies")
        if _blocking_item(item, dedicated=False)
    )
    return {item for item in result if item}


def dependent_ids(record):
    result = {
        _identifier(item)
        for item in _items(record, "dependents")
        if _blocking_item(item, dedicated=True)
    }
    # Some Beads JSON versions expose already-filtered blocking dependents.
    result.update(_identifier(item) for item in _items(record, "blocking_dependents"))
    return {item for item in result if item}


def _safe_reference(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _identifier(value)
    if isinstance(value, list):
        return sorted({item for entry in value if (item := _identifier(entry))})
    return None


def safe_record(record, relationship):
    """Copy the allowed planning fields exactly; all other fields stay private."""
    result = {"id": record["id"], "relationship": relationship}
    for field in SAFE_FIELDS:
        if field not in record or record[field] is None:
            continue
        value = record[field]
        if field in REFERENCE_FIELDS:
            value = _safe_reference(value)
        if isinstance(value, (str, list)) and value not in ("", []):
            result[field] = value
    return result


def build_snapshot(
    subject, read_record, *, max_records=MAX_RECORDS, max_bytes=MAX_BYTES
):
    """Select the local neighborhood and return canonical JSONL bytes and facts."""
    records = {subject["id"]: subject}
    relationships = {subject["id"]: "subject"}

    def load(identifier):
        if identifier not in records:
            record = read_record(identifier)
            if not isinstance(record, dict) or record.get("id") != identifier:
                raise RelatedWorkError(f"related record {identifier} is malformed")
            records[identifier] = record
        return records[identifier]

    def add(identifiers, relationship):
        for identifier in sorted(set(identifiers)):
            if identifier and identifier != subject["id"]:
                current = relationships.get(identifier)
                if (
                    current is None
                    or RELATIONSHIP_ORDER[relationship] < RELATIONSHIP_ORDER[current]
                ):
                    relationships[identifier] = relationship
        if len(relationships) > max_records:
            raise RelatedWorkError("related-work snapshot exceeds record limit")

    parent = parent_id(subject)
    if parent:
        add([parent], "parent")
        parent_record = load(parent)
        add(child_ids(parent_record) - {subject["id"]}, "sibling")
    add(blocker_ids(subject), "blocker")
    add(dependent_ids(subject), "dependent")

    ancestor = parent_id(load(parent)) if parent else None
    depth = 0
    while ancestor and depth < MAX_ANCESTORS:
        add([ancestor], "ancestor")
        ancestor = parent_id(load(ancestor))
        depth += 1

    ordered = sorted(
        relationships.items(), key=lambda item: (RELATIONSHIP_ORDER[item[1]], item[0])
    )
    filtered = [
        safe_record(load(identifier), relationship)
        for identifier, relationship in ordered
    ]
    raw = b"".join(
        (
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode()
        for record in filtered
    )
    if len(raw) > max_bytes:
        raise RelatedWorkError("related-work snapshot exceeds byte limit")
    return raw, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "media_type": MEDIA_TYPE,
        "record_count": len(filtered),
        "bytes": len(raw),
    }


def reference(path, facts):
    return {"path": str(path), **facts}


def validate_reference(value, *, expected_path=None):
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256", "media_type", "record_count", "bytes"}
        or not isinstance(value.get("path"), str)
        or not Path(value["path"]).is_absolute()
        or value.get("media_type") != MEDIA_TYPE
        or not isinstance(value.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        or not isinstance(value.get("record_count"), int)
        or isinstance(value.get("record_count"), bool)
        or not 1 <= value["record_count"] <= MAX_RECORDS
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or not 0 < value["bytes"] <= MAX_BYTES
    ):
        raise RelatedWorkError("related-work reference is malformed")
    path = Path(value["path"])
    if expected_path is not None and path.resolve() != Path(expected_path).resolve():
        raise RelatedWorkError("related-work path disagrees with the Run")
    return value


def snapshot_ids(value):
    """Validate one frozen reference and return its immutable record identities."""
    raw = validate_snapshot(value["path"], value)
    return {json.loads(line)["id"] for line in raw.splitlines()}


def validate_snapshot(path, value):
    validate_reference(value, expected_path=path)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RelatedWorkError("related-work snapshot is not a regular file")
    raw = path.read_bytes()
    if len(raw) != value["bytes"] or hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise RelatedWorkError("related-work snapshot digest disagrees")
    lines = raw.splitlines()
    if len(lines) != value["record_count"]:
        raise RelatedWorkError("related-work snapshot record count disagrees")
    records = []
    for line in lines:
        record = json.loads(line)
        if (
            not isinstance(record, dict)
            or set(record) - {"id", "relationship", *SAFE_FIELDS}
            or not isinstance(record.get("id"), str)
            or not record["id"]
            or record.get("relationship") not in RELATIONSHIP_ORDER
        ):
            raise RelatedWorkError("related-work snapshot contains unsafe fields")
        for field in PLANNING_TEXT_FIELDS:
            if field in record and not isinstance(record[field], str):
                raise RelatedWorkError("related-work snapshot contains unsafe fields")
        for field in REFERENCE_FIELDS:
            if field in record and not (
                isinstance(record[field], str)
                or isinstance(record[field], list)
                and all(isinstance(item, str) and item for item in record[field])
            ):
                raise RelatedWorkError("related-work snapshot contains unsafe fields")
        records.append(record)
    identities = [record["id"] for record in records]
    expected = sorted(
        records,
        key=lambda record: (RELATIONSHIP_ORDER[record["relationship"]], record["id"]),
    )
    canonical = b"".join(
        (
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode()
        for record in records
    )
    if (
        not records
        or records[0]["relationship"] != "subject"
        or len(set(identities)) != len(identities)
        or records != expected
        or raw != canonical
    ):
        raise RelatedWorkError("related-work snapshot is not canonical")
    return raw
