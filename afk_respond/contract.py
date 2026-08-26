"""Validate durable Feedback Response inputs and structured results."""

from pathlib import Path

from afk_config import validate_inference_setting


def validate_input(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("feedback response must use schema_version 1")
    workspace = value.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("feedback response workspace must be an absolute path")
    assessment_present = "assessment_directory" in value
    validation_present = "validation_directory" in value
    if assessment_present == validation_present:
        raise ValueError(
            "feedback response requires exactly one assessment or validation directory"
        )
    evidence_field = (
        "assessment_directory" if assessment_present else "validation_directory"
    )
    evidence_path = value[evidence_field]
    if not isinstance(evidence_path, str) or not Path(evidence_path).is_absolute():
        raise ValueError(f"feedback response {evidence_field} must be an absolute path")
    if validation_present:
        source = value.get("source")
        objective = value.get("objective")
        if (
            not isinstance(source, dict)
            or set(source) != {"kind", "directory"}
            or source.get("kind") not in {"attempt", "feedback_response"}
            or not isinstance(source.get("directory"), str)
            or not Path(source["directory"]).is_absolute()
        ):
            raise ValueError("validation repair source is malformed")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("validation repair objective must be a non-empty string")
    elif "source" in value or "objective" in value:
        raise ValueError(
            "review feedback response cannot contain validation repair fields"
        )
    if "inference" in value:
        validate_inference_setting(value["inference"])
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
