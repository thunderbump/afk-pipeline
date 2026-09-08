"""Pure verification and policy derivation for Iteration evidence."""

import json
from pathlib import Path

from afk_assess.contract import subject_state, validate_assessment
from afk_evidence.access import (
    MAX_RELATED_WORK_BYTES,
    EvidenceReader,
    EvidenceUnavailable,
)
from afk_evidence.stages import verify_change_lineage
from afk_related_work import validate_snapshot_bytes
from afk_review.contract import validate_review


def evaluate_policy(policy_input, *, reader=None, repository=None, verify_git=True):
    """Verify one assessment lineage and derive its deterministic policy."""
    assessment, lineage, protected_directories = verified_assessment(
        Path(policy_input["assessment_directory"]),
        reader=reader,
        repository=repository,
        verify_git=verify_git,
    )
    completed_responses = lineage.response_count
    actionable_findings = sum(
        decision["defect_decision"] == "confirmed"
        and decision["scope"]["kind"] == "current"
        for decision in assessment["decisions"]
    )
    return (
        decide(
            actionable_findings,
            completed_responses,
            policy_input["max_responses"],
        ),
        lineage,
        protected_directories,
    )


def validate_sealed_result(
    input_value, output_value, *, reader=None, repository=None, verify_git=True
):
    """Validate a sealed Iteration result against its complete evidence chain."""
    policy_input = validate_input(input_value)
    policy, lineage, _protected = evaluate_policy(
        policy_input,
        reader=reader,
        repository=repository,
        verify_git=verify_git,
    )
    expected_output = {
        "schema_version": 1,
        "outcome": "completed",
        "policy": policy,
    }
    if output_value != expected_output:
        raise ValueError("invalid sealed Iteration result")
    return policy_input, policy, lineage


def validate_input(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("iteration policy must use schema_version 1")
    if set(value) != {"schema_version", "assessment_directory", "max_responses"}:
        raise ValueError("iteration policy input has unexpected fields")
    directory = value.get("assessment_directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("assessment_directory must be an absolute path")
    limit = value.get("max_responses")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("max_responses must be a nonnegative integer")
    return value


def validate_result_location(result_directory, workspace, evidence_directories):
    result = result_directory.resolve()
    protected_directories = {workspace.resolve(), *evidence_directories}
    if any(
        result == protected or protected in result.parents
        for protected in protected_directories
    ):
        raise ValueError("result directory must be outside the workspace and evidence")


def verified_assessment(
    assessment_directory, reader=None, repository=None, verify_git=True
):
    # Standalone authority begins at the explicitly supplied Assessment only.
    reader = reader or EvidenceReader((assessment_directory.absolute(),))
    assessment_input = validate_stage_input(
        read_object(
            reader, assessment_directory / "input.json", "Finding Assessment input"
        ),
        "Finding Assessment",
        "review_directory",
    )
    assessment_output = read_object(
        reader, assessment_directory / "output.json", "Finding Assessment output"
    )
    if assessment_output.get("outcome") != "completed":
        raise ValueError("iteration policy requires a completed Finding Assessment")
    workspace_value = assessment_input["workspace"]
    review_directory = Path(assessment_input["review_directory"])
    reader.authorize_directory(review_directory)
    review_input = validate_stage_input(
        read_object(reader, review_directory / "input.json", "Review input"),
        "Review",
        "change_directory",
        "validation_directory",
    )
    review_output = read_object(
        reader, review_directory / "output.json", "Review output"
    )
    change_directory = Path(review_input["change_directory"])
    reader.authorize_directory(change_directory)
    lineage = verify_change_lineage(
        change_directory,
        reader=reader,
        repository=repository,
        verify_git=verify_git,
    )
    change_after = lineage.after

    try:
        repository = assessment_output["repository"]
        assessment_before = subject_state(repository["before"])
        assessment_after = subject_state(repository["after"])
        review_repository = review_output["repository"]
        review_before = subject_state(review_repository["before"])
        review_after = subject_state(review_repository["after"])
        review_value = review_output["review"]
        assessment_value = assessment_output["assessment"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid Finding Assessment evidence") from error
    if repository.get("unchanged") is not True or assessment_before != assessment_after:
        raise ValueError("completed Finding Assessment must be read-only")
    if review_output.get("outcome") != "completed":
        raise ValueError("Finding Assessment requires a completed Review")
    if review_repository.get("unchanged") is not True or review_before != review_after:
        raise ValueError("completed Review must be read-only")
    if not (
        assessment_after == review_after == subject_state(change_after)
        and not assessment_after["dirty"]
        and not assessment_after["status"]
    ):
        raise ValueError("iteration evidence must identify one clean reviewed state")
    workspace = Path(workspace_value)
    if not (
        Path(lineage.assignment["workspace"]).resolve()
        == workspace.resolve()
        == Path(review_input.get("workspace", "")).resolve()
    ):
        raise ValueError("iteration evidence workspaces must match")
    review_related = review_input.get("related_work")
    if assessment_input.get("related_work") != review_related:
        raise ValueError("Finding Assessment must use the Review related-work snapshot")
    related_work_ids = _snapshot_ids(reader, review_related)
    review = validate_review(
        review_value, workspace, review_after["head"], related_work_ids
    )
    evidence_directories = {
        assessment_directory.resolve(),
        review_directory.resolve(),
        Path(review_input["validation_directory"]).resolve(),
        *lineage.evidence_directories,
    }
    return (
        validate_assessment(review, assessment_value, related_work_ids),
        lineage,
        evidence_directories,
    )


def validate_stage_input(value, name, *evidence_fields):
    if value.get("schema_version") != 1:
        raise ValueError(f"{name} input must use schema_version 1")
    for field in ("workspace", *evidence_fields):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"invalid {name} input {field}")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"invalid {name} input timeout_seconds")
    return value


def decide(actionable_findings, completed_responses, max_responses):
    if actionable_findings == 0:
        return {
            "decision": "stop",
            "completed_responses": completed_responses,
            "max_responses": max_responses,
            "actionable_findings": actionable_findings,
            "reason": "the latest assessment has no actionable findings",
        }
    if completed_responses >= max_responses:
        return {
            "decision": "exhausted",
            "completed_responses": completed_responses,
            "max_responses": max_responses,
            "actionable_findings": actionable_findings,
            "reason": "the response limit has been reached",
        }
    return {
        "decision": "continue",
        "completed_responses": completed_responses,
        "max_responses": max_responses,
        "actionable_findings": actionable_findings,
        "next_response_number": completed_responses + 1,
        "reason": "actionable findings remain within the response limit",
    }


def read_object(reader, path, name):
    try:
        value = reader.json(path)
    except EvidenceUnavailable as error:
        raise ValueError(error.reason) from error
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _snapshot_ids(reader, reference):
    if reference is None:
        return set()
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise TypeError("related-work reference is malformed")
    try:
        reader.authorize_file(reference["path"])
        raw = reader.bytes(reference["path"], MAX_RELATED_WORK_BYTES)
    except EvidenceUnavailable as error:
        raise ValueError(error.reason) from error
    validate_snapshot_bytes(raw, reference)
    return {json.loads(line)["id"] for line in raw.splitlines()}
