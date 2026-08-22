"""Revalidate a published child and its scoped Completion Record."""

from afk_plan.contract import bounded_text, object_with_keys, string_list, utc_timestamp

PRODUCER_KINDS = {
    "pipeline_run",
    "repository_check",
    "external_check",
    "human_attestation",
    "human_waiver",
}
SUBJECT_FIELDS = {"commit", "environment"}


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
