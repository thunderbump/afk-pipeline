"""Verify Committed Change source evidence and expose its immutable lineage."""

import json
import re
import subprocess
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
from afk_related_work import validate_snapshot_bytes
from afk_respond.contract import actionable_findings, validate_response
from afk_respond.contract import validate_input as validate_response_input
from afk_review.contract import validate_output_projection
from afk_runtime import git
from afk_validate.evidence import (
    evidence_identity,
    load_passed_evidence,
    validate_repairable_failure,
)

from .access import (
    MAX_RELATED_WORK_BYTES,
    MAX_VALIDATION_LOG_BYTES,
    EvidenceReader,
    EvidenceUnavailable,
)


@dataclass
class VerifiedLineage:
    assignment: dict[str, object]
    before: dict[str, object]
    after: dict[str, object]
    response_count: int
    evidence_directories: set[Path] = field(default_factory=set)


def verify_source(
    kind, source_directory, *, reader=None, repository=None, verify_git=True
):
    directory = Path(source_directory).absolute()
    # A standalone caller owns the supplied stage directory, not its parent (or
    # grandparent). Further evidence is admitted only when a validated record
    # explicitly names it.
    lineage = _Lineage(reader or EvidenceReader((directory,)), repository, verify_git)
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


def verify_change_lineage(
    change_directory, *, reader=None, repository=None, verify_git=True
):
    directory = Path(change_directory).absolute()
    lineage = _Lineage(reader or EvidenceReader((directory,)), repository, verify_git)
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
    reader: EvidenceReader
    repository: Path | None = None
    verify_git: bool = True
    response_count: int = 0
    evidence_directories: set[Path] = field(default_factory=set)

    def include(self, directory, *, referenced=False):
        if referenced:
            self.reader.authorize_directory(directory)
        self.reader.relative(directory)
        self.evidence_directories.add(Path(directory).absolute())

    def read(self, path):
        # Missing and oversized proof must retain its two-state classification
        # all the way to read_run; structural contradictions alone are invalid.
        return self.reader.json(path)


def _committed_attempt(source_directory, lineage):
    lineage.include(source_directory)
    assignment = validate_assignment(lineage.read(source_directory / "input.json"))
    attempt = lineage.read(source_directory / "output.json")
    before, after = validate_attempt(attempt)
    if assignment.get("work_base", before["head"]) != before["head"]:
        raise ValueError("Assignment work_base disagrees with initial Attempt")
    validate_transition(
        Path(assignment["workspace"]), before, after, attempt["repository"], lineage
    )
    return assignment, before, after


def _committed_response(source_directory, visited, lineage):
    remember_evidence(visited, "feedback_response", source_directory)
    lineage.include(source_directory)
    lineage.response_count += 1
    response_input = validate_response_input(
        lineage.read(source_directory / "input.json")
    )
    response_output = lineage.read(source_directory / "output.json")
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
        lineage.include(Path(response_input["validation_directory"]), referenced=True)
        assignment = _validation_repair_source(response_input, before, visited, lineage)
        validate_response([], response_output.get("response"))
    else:
        assessment_directory = Path(response_input["assessment_directory"])
        lineage.include(assessment_directory, referenced=True)
        assessment_input = _object(
            lineage.read(assessment_directory / "input.json"),
            "Finding Assessment input",
        )
        assessment_output = _object(
            lineage.read(assessment_directory / "output.json"),
            "Finding Assessment output",
        )
        review_directory = absolute_evidence_path(assessment_input, "review_directory")
        lineage.include(review_directory, referenced=True)
        review_input = _object(
            lineage.read(review_directory / "input.json"), "Review input"
        )
        review_output = _object(
            lineage.read(review_directory / "output.json"), "Review output"
        )
        change_directory = absolute_evidence_path(review_input, "change_directory")
        validation_directory = absolute_evidence_path(
            review_input, "validation_directory"
        )
        lineage.include(change_directory, referenced=True)
        lineage.include(validation_directory, referenced=True)
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
            assessment_value = assessment_output["assessment"]
            response_value = response_output["response"]
        except KeyError as error:
            raise ValueError("invalid Feedback Response evidence") from error
        review_related = review_input.get("related_work")
        if assessment_input.get("related_work") != review_related:
            raise ValueError(
                "Finding Assessment must use the Review related-work snapshot"
            )
        if assignment.get("related_work") != review_related:
            raise ValueError("stage related-work evidence must match the Assignment")
        related_work_ids = _snapshot_ids(lineage, review_related)
        validation_input, validation_output, validation_stdout, validation_stderr = (
            load_passed_evidence(
                validation_directory,
                reader=lineage.reader,
                log_limit=MAX_VALIDATION_LOG_BYTES,
            )
        )
        if review_output.get("validation_evidence") != evidence_identity(
            validation_input,
            validation_output,
            validation_stdout,
            validation_stderr,
        ):
            raise ValueError("Review-bound Validation evidence identity disagrees")
        if (
            Path(validation_input["workspace"]).resolve() != workspace.resolve()
            or subject_state(validation_output["repository"]["before"])
            != subject_state(source_after)
            or subject_state(validation_output["repository"]["after"])
            != subject_state(source_after)
        ):
            raise ValueError("Validation and reviewed Change subjects disagree")
        reviewed = validate_output_projection(
            review_output,
            workspace,
            before["head"],
            related_work_ids,
            review_directory,
            lineage.reader,
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
    validate_transition(workspace, before, after, response_repository, lineage)
    return assignment, before, after


def _validation_repair_source(response_input, response_before, visited, lineage):
    validation_directory = Path(response_input["validation_directory"])
    lineage.include(validation_directory)
    _validation_input, validation_output = validate_repairable_failure(
        validation_directory,
        Path(response_input["workspace"]),
        reader=lineage.reader,
        log_limit=MAX_VALIDATION_LOG_BYTES,
    )
    source = response_input["source"]
    source_directory = Path(source["directory"])
    lineage.include(source_directory, referenced=True)
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
    recorded = validate_change_output(lineage.read(change_directory / "output.json"))
    source = recorded["source"]
    source_directory = Path(source["directory"])
    lineage.include(source_directory, referenced=True)
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


def validate_transition(workspace, before, after, repository, lineage):
    git_workspace = lineage.repository or workspace
    if before["head"] == after["head"]:
        raise ValueError("committed change requires distinct repository heads")
    recorded = repository.get("commits_between_heads")
    if (
        not isinstance(recorded, list)
        or not recorded
        or not all(isinstance(commit, str) and commit for commit in recorded)
    ):
        raise ValueError("committed change requires a recorded commit range")
    revisions = (before["head"], after["head"], *recorded)
    if any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None
        for revision in revisions
    ):
        raise ValueError(
            "Committed Change revisions must be canonical commit object IDs"
        )
    if lineage.verify_git:
        # Probe every recorded object before graph operations. A locally missing
        # intermediate object is unavailable proof, not malformed lineage.
        for revision in revisions:
            present = subprocess.run(
                [
                    "git",
                    "-C",
                    str(git_workspace),
                    "cat-file",
                    "-e",
                    f"{revision}^{{commit}}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if present.returncode:
                raise EvidenceUnavailable(
                    "recorded Git object is unavailable", revision
                )
            require_canonical_commit(git_workspace, revision)
        validate_git_transition(git_workspace, before, after)
        actual = git(
            git_workspace, "rev-list", "--reverse", f"{before['head']}..{after['head']}"
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


def _object(value, name):
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _snapshot_ids(lineage, reference):
    if reference is None:
        return set()
    path = reference.get("path") if isinstance(reference, dict) else None
    if not isinstance(path, str):
        raise TypeError("related-work reference is malformed")
    lineage.reader.authorize_file(path)
    raw = lineage.reader.bytes(path, MAX_RELATED_WORK_BYTES)
    validate_snapshot_bytes(raw, reference)
    return {json.loads(line)["id"] for line in raw.splitlines()}
