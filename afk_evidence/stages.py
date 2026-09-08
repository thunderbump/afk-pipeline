"""Verify Committed Change source evidence and expose its immutable lineage."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from afk_assess.contract import subject_state, validate_assessment
from afk_attempt.contract import validate_assignment
from afk_change.contract import (
    require_canonical_commit,
    validate_change_output,
    validate_git_transition,
    validate_repository_state,
)
from afk_related_work import snapshot_ids
from afk_respond.contract import actionable_findings, validate_response
from afk_respond.contract import validate_input as validate_response_input
from afk_review.contract import validate_review
from afk_runtime import git
from afk_validate.evidence import validate_repairable_failure


@dataclass
class VerifiedLineage:
    assignment: dict[str, object]
    before: dict[str, object]
    after: dict[str, object]
    response_count: int
    evidence_directories: set[Path] = field(default_factory=set)


def verify_source(kind, source_directory):
    lineage = _Lineage()
    if kind == "attempt":
        assignment, before, after = _committed_attempt(source_directory, lineage)
    else:
        assignment, before, after = _committed_response(
            source_directory, set(), lineage
        )
    return VerifiedLineage(
        assignment,
        before,
        after,
        lineage.response_count,
        lineage.evidence_directories,
    )


def verify_change_lineage(change_directory):
    lineage = _Lineage()
    assignment, before, after = _committed_change(change_directory, set(), lineage)
    return VerifiedLineage(
        assignment,
        before,
        after,
        lineage.response_count,
        lineage.evidence_directories,
    )


@dataclass
class _Lineage:
    response_count: int = 0
    evidence_directories: set[Path] = field(default_factory=set)

    def include(self, directory):
        self.evidence_directories.add(directory.resolve())


def _committed_attempt(source_directory, lineage):
    lineage.include(source_directory)
    assignment = validate_assignment(read_json(source_directory / "input.json"))
    attempt = read_json(source_directory / "output.json")
    before, after = validate_attempt(attempt)
    validate_transition(
        Path(assignment["workspace"]), before, after, attempt["repository"]
    )
    return assignment, before, after


def _committed_response(source_directory, visited, lineage):
    remember_evidence(visited, "feedback_response", source_directory)
    lineage.include(source_directory)
    lineage.response_count += 1
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
    workspace = Path(response_input["workspace"])

    if "validation_directory" in response_input:
        assignment = _validation_repair_source(response_input, before, visited, lineage)
        validate_response([], response_output.get("response"))
    else:
        assessment_directory = Path(response_input["assessment_directory"])
        lineage.include(assessment_directory)
        assessment_input = read_object(
            assessment_directory / "input.json", "Finding Assessment input"
        )
        assessment_output = read_object(
            assessment_directory / "output.json", "Finding Assessment output"
        )
        review_directory = absolute_evidence_path(assessment_input, "review_directory")
        lineage.include(review_directory)
        review_input = read_object(review_directory / "input.json", "Review input")
        review_output = read_object(review_directory / "output.json", "Review output")
        change_directory = absolute_evidence_path(review_input, "change_directory")
        validation_directory = absolute_evidence_path(
            review_input, "validation_directory"
        )
        lineage.include(validation_directory)
        assignment, _source_before, source_after = _committed_change(
            change_directory, visited, lineage
        )

        require_same_workspace(workspace, assignment, assessment_input, review_input)
        assessed_state = validate_read_only_stage(
            assessment_output, "Finding Assessment"
        )
        reviewed_state = validate_read_only_stage(review_output, "Review")
        if not (
            assessed_state
            == reviewed_state
            == subject_state(source_after)
            == subject_state(before)
        ):
            raise ValueError(
                "Feedback Response evidence must identify one source state"
            )
        try:
            review_value = review_output["review"]
            assessment_value = assessment_output["assessment"]
            response_value = response_output["response"]
        except KeyError as error:
            raise ValueError("invalid Feedback Response evidence") from error
        review_related = review_input.get("related_work")
        if assessment_input.get("related_work") != review_related:
            raise ValueError(
                "Finding Assessment must use the Review related-work snapshot"
            )
        related_work_ids = (
            snapshot_ids(review_related) if review_related is not None else set()
        )
        reviewed = validate_review(
            review_value, workspace, before["head"], related_work_ids
        )
        assessed = validate_assessment(reviewed, assessment_value, related_work_ids)
        selected = actionable_findings(reviewed, assessed)
        if not selected:
            raise ValueError(
                "committed change requires an actionable Feedback Response"
            )
        validate_response(selected, response_value)

    if response_repository.get("descends_from_before") is not True:
        raise ValueError("Feedback Response must record descendant commits")
    validate_transition(workspace, before, after, response_repository)
    return assignment, before, after


def _validation_repair_source(response_input, response_before, visited, lineage):
    validation_directory = Path(response_input["validation_directory"])
    lineage.include(validation_directory)
    _validation_input, validation_output = validate_repairable_failure(
        validation_directory, Path(response_input["workspace"])
    )
    source = response_input["source"]
    source_directory = Path(source["directory"])
    if source["kind"] == "attempt":
        assignment, _source_before, source_after = _committed_attempt(
            source_directory, lineage
        )
    else:
        assignment, _source_before, source_after = _committed_response(
            source_directory, visited, lineage
        )
    validation_state = clean_repository_state(validation_output["repository"]["after"])
    if not (
        subject_state(source_after)
        == subject_state(validation_state)
        == subject_state(response_before)
    ):
        raise ValueError("validation repair evidence must identify one source state")
    if (
        Path(assignment["workspace"]).resolve()
        != Path(response_input["workspace"]).resolve()
        or assignment["objective"] != response_input["objective"]
    ):
        raise ValueError("validation repair does not match its Assignment")
    return assignment


def _committed_change(change_directory, visited, lineage):
    remember_evidence(visited, "committed_change", change_directory)
    lineage.include(change_directory)
    recorded = validate_change_output(read_json(change_directory / "output.json"))
    source = recorded["source"]
    source_directory = Path(source["directory"])
    if source["kind"] == "attempt":
        assignment, before, after = _committed_attempt(source_directory, lineage)
    else:
        assignment, before, after = _committed_response(
            source_directory, visited, lineage
        )
    if (
        recorded["objective"] != assignment["objective"]
        or Path(recorded["workspace"]).resolve()
        != Path(assignment["workspace"]).resolve()
        or recorded["repository"] != {"before": before, "after": after}
    ):
        raise ValueError("Committed Change does not match its source evidence")
    return assignment, before, after


def validate_attempt(value):
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


def remember_evidence(visited, kind, directory):
    evidence = (kind, directory.resolve())
    if evidence in visited:
        raise ValueError("Feedback Response evidence chain contains a cycle")
    visited.add(evidence)


def clean_repository_state(value):
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


def read_json(path):
    return json.loads(path.read_text())
