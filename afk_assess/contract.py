"""Validate structured Finding Assessment results against an immutable Review."""


def subject_state(state: dict[str, object]) -> dict[str, object]:
    if not isinstance(state, dict):
        raise TypeError("invalid Review evidence repository state")
    try:
        subject = {field: state[field] for field in ("head", "dirty", "status")}
    except KeyError as error:
        raise ValueError("invalid Review evidence repository state") from error
    if (
        not isinstance(subject["head"], str)
        or not subject["head"]
        or not isinstance(subject["dirty"], bool)
        or not isinstance(subject["status"], list)
        or not all(isinstance(line, str) for line in subject["status"])
    ):
        raise ValueError("invalid Review evidence repository state")
    return subject


def validate_assessment(
    review: dict[str, object],
    value: object,
    related_work_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("assessment response must be an object")
    if list(value) != ["summary", "decisions"]:
        raise ValueError("assessment response fields are malformed or out of order")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("assessment summary must be a non-empty string")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise TypeError("assessment decisions must be an array")
    expected = set(range(len(review["findings"])))
    seen = set()
    for decision in decisions:
        validate_decision(decision, expected, seen, related_work_ids)
    if seen != expected:
        raise ValueError("each Review finding must have one decision")
    return value


def validate_decision(
    decision: object,
    expected: set[int],
    seen: set[int],
    related_work_ids: set[str] | frozenset[str] = frozenset(),
) -> None:
    if not isinstance(decision, dict):
        raise TypeError("each assessment decision must be an object")
    if list(decision) != ["finding_index", "defect_decision", "rationale", "scope"]:
        raise ValueError("assessment decision fields are malformed or out of order")
    index = decision.get("finding_index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("decision finding_index must be an integer")
    if index not in expected or index in seen:
        raise ValueError("each Review finding must have one decision")
    if decision.get("defect_decision") not in {"confirmed", "rejected"}:
        raise ValueError("decision defect_decision must be confirmed or rejected")
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("decision rationale must be a non-empty string")
    validate_scope(decision.get("scope"), related_work_ids)
    seen.add(index)


def validate_scope(
    value: object, related_work_ids: set[str] | frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("decision scope must be an object")
    kind = value.get("kind")
    expected_fields = (
        ["kind", "rationale", "related_work_id"]
        if kind == "related"
        else ["kind", "rationale"]
    )
    if list(value) != expected_fields:
        raise ValueError("decision scope fields are malformed or out of order")
    if kind not in {"current", "related", "unknown"}:
        raise ValueError("decision scope kind must be current, related, or unknown")
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("decision scope rationale must be a non-empty string")
    if kind == "related":
        related_id = value.get("related_work_id")
        if not isinstance(related_id, str) or related_id not in related_work_ids:
            raise ValueError("decision related_work_id must exist in related work")
    return value
