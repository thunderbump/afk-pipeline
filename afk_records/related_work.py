"""Retained-record related work validation and projection."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MEDIA_TYPE = "application/x-ndjson"

SNAPSHOT_NAME = "related-work.jsonl"

MAX_RECORDS = 64

MAX_BYTES = 256 * 1024

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


class RelatedWorkError(ValueError):
    pass


def canonical_bytes(records):
    return b"".join(
        (
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode()
        for record in records
    )


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


def validate_snapshot(path, value):
    validate_reference(value, expected_path=path)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RelatedWorkError("related-work snapshot is not a regular file")
    return validate_snapshot_bytes(path.read_bytes(), value)


def validate_snapshot_bytes(raw, value):
    """Validate already safely-read snapshot bytes against the canonical contract."""
    validate_reference(value)
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
            or set(record) - {"id", "relationship", "selection", *SAFE_FIELDS}
            or not isinstance(record.get("id"), str)
            or not record["id"]
            or record.get("relationship") not in RELATIONSHIP_ORDER
        ):
            raise RelatedWorkError("related-work snapshot contains unsafe fields")
        if "selection" in record:
            selection = record["selection"]
            if (
                record["relationship"] != "subject"
                or not isinstance(selection, dict)
                or set(selection) != {"version", "omitted_records"}
                or type(selection["version"]) is not int
                or selection["version"] != 1
                or type(selection["omitted_records"]) is not int
                or not 1 <= selection["omitted_records"] <= 2**63 - 1
            ):
                raise RelatedWorkError("related-work selection metadata is malformed")
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
    canonical = canonical_bytes(records)
    if (
        not records
        or records[0]["relationship"] != "subject"
        or len(set(identities)) != len(identities)
        or records != expected
        or raw != canonical
    ):
        raise RelatedWorkError("related-work snapshot is not canonical")
    return raw
