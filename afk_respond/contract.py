"""Validate durable Feedback Response inputs and structured results."""

from pathlib import Path


def validate_input(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("feedback response must use schema_version 1")
    for field in ("workspace", "assessment_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"feedback response {field} must be an absolute path")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("feedback response timeout_seconds must be a positive integer")
    return value


def actionable_findings(review, assessment):
    return [
        {
            "finding_index": decision["finding_index"],
            "finding": review["findings"][decision["finding_index"]],
            "assessment_rationale": decision["rationale"],
        }
        for decision in assessment["decisions"]
        if decision["worth_addressing"]
    ]


def validate_response(selected, value):
    if not isinstance(value, dict):
        raise TypeError("feedback response must be an object")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("feedback response summary must be a non-empty string")
    responses = value.get("finding_responses")
    if not isinstance(responses, list):
        raise TypeError("finding_responses must be an array")
    expected = {item["finding_index"] for item in selected}
    seen = set()
    for item in responses:
        if not isinstance(item, dict):
            raise TypeError("each finding response must be an object")
        index = item.get("finding_index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError("response finding_index must be an integer")
        if index not in expected or index in seen:
            raise ValueError("each actionable finding must have one response")
        if not isinstance(item.get("response"), str) or not item["response"].strip():
            raise ValueError("finding response must be a non-empty string")
        seen.add(index)
    if seen != expected:
        raise ValueError("each actionable finding must have one response")
    return value
