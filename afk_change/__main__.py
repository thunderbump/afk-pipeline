import json
import subprocess
import sys
from pathlib import Path

from afk_assess.contract import subject_state, validate_assessment
from afk_attempt.contract import validate_assignment
from afk_change.contract import (
    require_canonical_commit,
    validate_change_output,
    validate_git_transition,
    validate_repository_state,
)
from afk_respond.contract import actionable_findings, validate_response
from afk_respond.contract import (
    validate_input as validate_response_input,
)
from afk_review.contract import validate_review
from afk_runtime import git, progress, seal_json, write_json

USAGE = "usage: python3 -m afk_change SOURCE_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Project successful AFK evidence from committed-change source JSON into one result.

Arguments:
  SOURCE_JSON       Path to the committed-change source JSON file.
  RESULT_DIRECTORY  New directory where committed-change input and output are written.
"""


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    progress("loading committed-change input")
    change_input = json.loads(input_path.read_text())
    source = validate_input(change_input)
    progress("committed-change input accepted")
    source_directory = Path(source["directory"])
    progress(f"loading and verifying {source['kind']} evidence")
    if source["kind"] == "attempt":
        assignment, before, after = committed_attempt(source_directory)
    else:
        assignment, before, after = committed_response(source_directory)

    validate_result_location(
        result_directory, Path(assignment["workspace"]), source_directory
    )

    output = {
        "schema_version": 1,
        "outcome": "completed",
        "change": {
            "objective": assignment["objective"],
            "workspace": assignment["workspace"],
            "repository": {"before": before, "after": after},
            "source": source,
        },
    }
    progress("preparing committed-change result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", change_input)
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed completed committed-change outcome at {output_path}")
    return 0


def validate_input(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("committed change must use schema_version 1")
    source = value.get("source")
    if not isinstance(source, dict):
        raise TypeError("committed change source must be an object")
    if source.get("kind") not in {"attempt", "feedback_response"}:
        raise ValueError("committed change source kind is invalid")
    directory = source.get("directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("committed change source directory must be an absolute path")
    return {"kind": source["kind"], "directory": directory}


def validate_attempt(value: object) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Attempt output must use schema_version 1")
    if value.get("outcome") != "succeeded":
        raise ValueError("committed change requires a succeeded Attempt")
    repository = value.get("repository")
    if not isinstance(repository, dict):
        raise TypeError("invalid Attempt repository evidence")
    return clean_repository_state(repository.get("before")), clean_repository_state(
        repository.get("after")
    )


def committed_attempt(source_directory):
    assignment = validate_assignment(read_json(source_directory / "input.json"))
    attempt = read_json(source_directory / "output.json")
    before, after = validate_attempt(attempt)
    validate_transition(
        Path(assignment["workspace"]), before, after, attempt["repository"]
    )
    return assignment, before, after


def committed_response(source_directory, visited=None):
    visited = set() if visited is None else visited
    remember_evidence(visited, "feedback_response", source_directory)
    response_input = validate_response_input(read_json(source_directory / "input.json"))
    response_output = read_json(source_directory / "output.json")
    if (
        not isinstance(response_output, dict)
        or response_output.get("schema_version") != 1
    ):
        raise ValueError("Feedback Response output must use schema_version 1")
    if response_output.get("outcome") != "completed":
        raise ValueError("committed change requires a completed Feedback Response")
    response_repository = response_output.get("repository")
    if not isinstance(response_repository, dict):
        raise TypeError("invalid Feedback Response repository evidence")
    before = clean_repository_state(response_repository.get("before"))
    after = clean_repository_state(response_repository.get("after"))

    assessment_directory = Path(response_input["assessment_directory"])
    assessment_input = read_object(
        assessment_directory / "input.json", "Finding Assessment input"
    )
    assessment_output = read_object(
        assessment_directory / "output.json", "Finding Assessment output"
    )
    review_directory = absolute_evidence_path(assessment_input, "review_directory")
    review_input = read_object(review_directory / "input.json", "Review input")
    review_output = read_object(review_directory / "output.json", "Review output")
    change_directory = absolute_evidence_path(review_input, "change_directory")
    assignment, _source_before, source_after = committed_change(
        change_directory, visited
    )

    workspace = Path(response_input["workspace"])
    require_same_workspace(workspace, assignment, assessment_input, review_input)
    assessed_state = validate_read_only_stage(assessment_output, "Finding Assessment")
    reviewed_state = validate_read_only_stage(review_output, "Review")
    if not (
        assessed_state
        == reviewed_state
        == subject_state(source_after)
        == subject_state(before)
    ):
        raise ValueError("Feedback Response evidence must identify one source state")
    try:
        review_value = review_output["review"]
        assessment_value = assessment_output["assessment"]
        response_value = response_output["response"]
    except KeyError as error:
        raise ValueError("invalid Feedback Response evidence") from error
    reviewed = validate_review(review_value, workspace, before["head"])
    assessed = validate_assessment(reviewed, assessment_value)
    selected = actionable_findings(reviewed, assessed)
    if not selected:
        raise ValueError("committed change requires an actionable Feedback Response")
    validate_response(selected, response_value)
    if response_repository.get("descends_from_before") is not True:
        raise ValueError("Feedback Response must record descendant commits")
    validate_transition(workspace, before, after, response_repository)
    return assignment, before, after


def committed_change(change_directory, visited):
    remember_evidence(visited, "committed_change", change_directory)
    recorded = validate_change_output(read_json(change_directory / "output.json"))
    source = recorded["source"]
    source_directory = Path(source["directory"])
    if source["kind"] == "attempt":
        assignment, before, after = committed_attempt(source_directory)
    else:
        assignment, before, after = committed_response(source_directory, visited)
    if (
        recorded["objective"] != assignment["objective"]
        or Path(recorded["workspace"]).resolve()
        != Path(assignment["workspace"]).resolve()
        or recorded["repository"] != {"before": before, "after": after}
    ):
        raise ValueError("Committed Change does not match its source evidence")
    return assignment, before, after


def remember_evidence(visited, kind, directory):
    evidence = (kind, directory.resolve())
    if evidence in visited:
        raise ValueError("Feedback Response evidence chain contains a cycle")
    visited.add(evidence)


def clean_repository_state(value: object) -> dict[str, object]:
    state = validate_repository_state(value)
    if state["dirty"] or state["status"]:
        raise ValueError("committed change requires clean repository states")
    return state


def validate_transition(workspace, before, after, repository):
    validate_git_transition(workspace, before, after)
    if before["head"] == after["head"]:
        raise ValueError("committed change requires distinct repository heads")
    recorded = repository.get("commits_between_heads")
    if (
        not isinstance(recorded, list)
        or not recorded
        or not all(isinstance(commit, str) and commit for commit in recorded)
    ):
        raise ValueError("committed change requires a recorded commit range")
    for revision in recorded:
        require_canonical_commit(workspace, revision)
    actual = git(
        workspace, "rev-list", "--reverse", f"{before['head']}..{after['head']}"
    ).splitlines()
    if recorded != actual:
        raise ValueError("recorded commit range does not match the repository")


def validate_result_location(result_directory, workspace, source_directory):
    result = result_directory.resolve()
    for protected, message in (
        (workspace.resolve(), "result directory must be outside the source workspace"),
        (
            source_directory.resolve(),
            "result directory must be outside the source evidence directory",
        ),
    ):
        if result == protected or protected in result.parents:
            raise ValueError(message)


def validate_read_only_stage(value, name):
    if value.get("outcome") != "completed":
        raise ValueError(f"committed change requires a completed {name}")
    repository = value.get("repository")
    if not isinstance(repository, dict) or repository.get("unchanged") is not True:
        raise ValueError(f"completed {name} must be read-only")
    before = subject_state(repository.get("before"))
    after = subject_state(repository.get("after"))
    if before != after or before["dirty"] or before["status"]:
        raise ValueError(f"completed {name} must identify one clean state")
    return before


def require_same_workspace(workspace, assignment, *inputs):
    expected = workspace.resolve()
    values = [
        assignment.get("workspace"),
        *(value.get("workspace") for value in inputs),
    ]
    if any(
        not isinstance(value, str) or Path(value).resolve() != expected
        for value in values
    ):
        raise ValueError("Feedback Response evidence workspaces must match")


def absolute_evidence_path(value, field):
    path = value.get(field)
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError(f"invalid Feedback Response evidence {field}")
    return Path(path)


def read_object(path, name):
    value = read_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def read_json(path: Path) -> object:
    return json.loads(path.read_text())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-change: {error}", file=sys.stderr)
        raise SystemExit(2)
