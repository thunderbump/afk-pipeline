"""PROTOTYPE: pure validation for a Review finding assessment."""


def validate_assessment(review: dict[str, object], value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("assessment must be an object")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("assessment summary must be a non-empty string")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise TypeError("assessment decisions must be an array")

    findings = review.get("findings")
    if not isinstance(findings, list):
        raise TypeError("review findings must be an array")
    expected = set(range(len(findings)))
    seen = set()
    for decision in decisions:
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
    if seen != expected:
        raise ValueError("each Review finding must have one decision")
    return value
