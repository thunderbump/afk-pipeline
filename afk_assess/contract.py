"""Validate structured Finding Assessment results against a Review."""


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


def validate_assessment(review: dict[str, object], value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("assessment response must be an object")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("assessment summary must be a non-empty string")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise TypeError("assessment decisions must be an array")
    expected = set(range(len(review["findings"])))
    seen = set()
    for decision in decisions:
        validate_decision(decision, expected, seen)
    if seen != expected:
        raise ValueError("each Review finding must have one decision")
    return value


def validate_decision(decision: object, expected: set[int], seen: set[int]) -> None:
    if not isinstance(decision, dict):
        raise TypeError("each assessment decision must be an object")
    index = decision.get("finding_index")
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("decision finding_index must be an integer")
    if index not in expected or index in seen:
        raise ValueError("each Review finding must have one decision")
    if not isinstance(decision.get("worth_addressing"), bool):
        raise TypeError("decision worth_addressing must be a boolean")
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("decision rationale must be a non-empty string")
    seen.add(index)
