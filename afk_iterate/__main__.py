import json
import subprocess
import sys
from pathlib import Path

from afk_assess.contract import subject_state, validate_assessment
from afk_change.evidence import verify_change_lineage
from afk_related_work import snapshot_ids
from afk_review.contract import validate_review
from afk_runtime import progress, seal_json, write_json

USAGE = "usage: python3 -m afk_iterate POLICY_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Decide bounded review-response iteration from the latest completed Finding Assessment.

Arguments:
  POLICY_JSON      Path to the iteration-policy JSON file.
  RESULT_DIRECTORY New directory where policy input and output are written.
"""


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    progress("loading iteration-policy input")
    policy_input = validate_input(read_json(input_path))
    progress("iteration-policy input accepted")
    progress("loading and verifying Finding Assessment evidence")
    policy, lineage, protected_directories = evaluate_policy(policy_input)
    validate_result_location(
        result_directory,
        Path(lineage.assignment["workspace"]),
        protected_directories,
    )

    progress("preparing iteration-policy result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", policy_input)
    output = {"schema_version": 1, "outcome": "completed", "policy": policy}
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed completed iteration-policy outcome at {output_path}")
    return 0


def evaluate_policy(policy_input):
    """Verify one assessment lineage and derive its deterministic policy."""
    assessment, lineage, protected_directories = verified_assessment(
        Path(policy_input["assessment_directory"])
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


def validate_sealed_result(input_value, output_value):
    """Validate a sealed Iteration result against its complete evidence chain."""
    policy_input = validate_input(input_value)
    policy, lineage, _protected = evaluate_policy(policy_input)
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


def verified_assessment(assessment_directory):
    assessment_input = validate_stage_input(
        read_object(assessment_directory / "input.json", "Finding Assessment input"),
        "Finding Assessment",
        "review_directory",
    )
    assessment_output = read_object(
        assessment_directory / "output.json", "Finding Assessment output"
    )
    if assessment_output.get("outcome") != "completed":
        raise ValueError("iteration policy requires a completed Finding Assessment")
    workspace_value = assessment_input["workspace"]
    review_directory = Path(assessment_input["review_directory"])
    review_input = validate_stage_input(
        read_object(review_directory / "input.json", "Review input"),
        "Review",
        "change_directory",
        "validation_directory",
    )
    review_output = read_object(review_directory / "output.json", "Review output")
    change_directory = Path(review_input["change_directory"])
    lineage = verify_change_lineage(change_directory)
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
    related_work_ids = (
        snapshot_ids(review_related) if review_related is not None else set()
    )
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


def read_object(path, name):
    value = read_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def read_json(path):
    return json.loads(path.read_text())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-iterate: {error}", file=sys.stderr)
        raise SystemExit(2)
