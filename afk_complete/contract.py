"""Revalidate a published child and its scoped Completion Record."""

import json
from pathlib import Path

from afk_plan.contract import bounded_text, object_with_keys, string_list, utc_timestamp

PRODUCER_KINDS = {
    "pipeline_run",
    "repository_check",
    "external_check",
    "human_attestation",
    "human_waiver",
}
SUBJECT_FIELDS = {"commit", "environment"}
OUTPUT_FIELDS = {
    "schema_version",
    "outcome",
    "decision",
    "source",
    "started_at",
    "finished_at",
    "duration_seconds",
    "acceptance_sha256",
    "plan_sha256",
    "local_id",
    "criteria",
    "evidence_basis",
    "satisfies_criteria",
    "record",
    "error_category",
    "artifacts",
}


def validate_record(
    value: object,
    child: dict[str, object],
    child_id: str,
    plan_sha256: str,
    expected_subject: dict[str, str],
) -> tuple[dict[str, object], str, bool]:
    record = object_with_keys(
        value,
        {
            "schema_version",
            "child",
            "parent_plan",
            "outcome",
            "producer",
            "criteria",
            "subject",
            "evidence",
            "accepted_at",
        },
        "Completion Record",
    )
    producer = object_with_keys(
        record["producer"], {"kind", "identity"}, "Completion Record producer"
    )
    kind = producer["kind"]
    if kind not in PRODUCER_KINDS:
        raise ValueError("Completion Record producer kind is invalid")
    bounded_text(producer["identity"], "Completion Record producer identity", 256)
    allowed_kinds = {child["evidence_route"]}
    if child["evidence_route"] == "human_attestation":
        allowed_kinds.add("human_waiver")
    expected_outcome = "waived" if kind == "human_waiver" else "satisfied"
    if (
        record["schema_version"] != 1
        or record["child"] != child_id
        or record["parent_plan"] != plan_sha256
        or record["outcome"] != expected_outcome
        or kind not in allowed_kinds
        or producer["identity"] != child["owner"]
    ):
        raise ValueError("Completion Record does not match its accepted child")
    criteria = string_list(record["criteria"], "Completion Record criteria", 128, 256)
    if criteria != child["criteria"]:
        raise ValueError("Completion Record criteria do not match the child")
    subject = validate_subject(record["subject"], "Completion Record subject")
    if subject != expected_subject:
        raise ValueError("Completion Record subject is stale")
    required_subject = (
        set(child["handoff"]["subject_fields"])
        if child["execution"] != "agent"
        else set(expected_subject)
    )
    if set(subject) != required_subject:
        raise ValueError("Completion Record subject fields do not match the child")
    record["evidence"] = string_list(
        record["evidence"], "Completion Record evidence", 16, 2048
    )
    if not record["evidence"]:
        raise ValueError("Completion Record evidence must not be empty")
    utc_timestamp(record["accepted_at"], "Completion Record accepted_at")
    record["producer"] = producer
    record["criteria"] = criteria
    record["subject"] = subject
    satisfies = kind != "human_waiver"
    return record, kind, satisfies


def validate_subject(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value or not set(value) <= SUBJECT_FIELDS:
        raise ValueError(f"{name} has invalid fields")
    accepted = dict(value)
    for field, item in accepted.items():
        bounded_text(item, f"{name}.{field}", 1024)
    return accepted


def validate_output(
    value: object,
    child: dict[str, object],
    child_id: str,
    acceptance_sha256: str,
    plan_sha256: str,
    expected_subject: dict[str, str],
) -> dict[str, object]:
    """Validate a sealed successful Completion Validator result for fan-in."""
    output = object_with_keys(value, OUTPUT_FIELDS, "Completion Validator output")
    record, basis, satisfies = validate_record(
        output["record"],
        child,
        child_id,
        plan_sha256,
        expected_subject,
    )
    if (
        output["schema_version"] != 1
        or output["outcome"] != "completed"
        or output["decision"] != record["outcome"]
        or output["source"] != {"kind": "bead", "id": child_id}
        or output["acceptance_sha256"] != acceptance_sha256
        or output["plan_sha256"] != plan_sha256
        or output["local_id"] != child["local_id"]
        or output["criteria"] != child["criteria"]
        or output["evidence_basis"] != basis
        or output["satisfies_criteria"] is not satisfies
        or output["error_category"] is not None
        or output["artifacts"] != {"input": "input.json"}
    ):
        raise ValueError("Completion Validator output does not match its child")
    for field in ("started_at", "finished_at"):
        utc_timestamp(output[field], f"Completion Validator {field}")
    duration = output["duration_seconds"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("Completion Validator duration_seconds is invalid")
    output["record"] = record
    return output


def load_result(
    directory: Path,
    child: dict[str, object],
    child_id: str,
    acceptance_sha256: str,
    plan_sha256: str,
    acceptance_directory: Path,
    publication_directory: Path,
    expected_subject: dict[str, str],
) -> dict[str, object]:
    """Load and revalidate one immutable Completion Validator attempt."""
    request = object_with_keys(
        json.loads((directory / "input.json").read_text()),
        {
            "schema_version",
            "acceptance_directory",
            "publication_directory",
            "expected_subject",
            "record",
        },
        "Completion Validator input",
    )
    if (
        request["schema_version"] != 1
        or not Path(request["acceptance_directory"]).is_absolute()
        or not Path(request["publication_directory"]).is_absolute()
        or Path(request["acceptance_directory"]).resolve() != acceptance_directory
        or Path(request["publication_directory"]).resolve() != publication_directory
    ):
        raise ValueError("Completion Validator input does not match parent evidence")
    subject = validate_subject(request["expected_subject"], "expected_subject")
    if subject != expected_subject:
        raise ValueError("Completion Validator subject is stale at parent review")
    output = validate_output(
        json.loads((directory / "output.json").read_text()),
        child,
        child_id,
        acceptance_sha256,
        plan_sha256,
        subject,
    )
    if output["record"] != request["record"]:
        raise ValueError("Completion Validator output does not match its input")
    return output
