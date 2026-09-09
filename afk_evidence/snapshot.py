"""Read-only, independently revalidated local Run evidence."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from afk_assess.contract import validate_assessment
from afk_assess.contract import validate_input as validate_assessment_input
from afk_attempt.contract import validate_assignment
from afk_change.contract import validate_change_output, validate_repository_state
from afk_coordinate.contract import (
    COMPONENT_TOPOLOGY,
    validate_checkpoint,
    validate_component_output,
    validate_output,
    validate_request,
)
from afk_related_work import validate_snapshot_bytes
from afk_review.contract import validate_input as validate_review_input
from afk_review.contract import validate_review
from afk_validate.evidence import (
    evidence_identity,
    load_passed_evidence,
    validate_repairable_failure,
)

from .access import (
    MAX_RELATED_WORK_BYTES,
    MAX_VALIDATION_LOG_BYTES,
    EvidenceAccessError,
    EvidenceReader,
    EvidenceUnavailable,
)
from .continuation import observe_lineage, require_terminal_pair


class RunValidationError(EvidenceAccessError):
    """Evidence exists but is malformed, contradictory, or unsafe."""


_Unavailable = EvidenceUnavailable


@dataclass(frozen=True)
class ProofResult:
    status: Literal["verified", "unavailable"]
    reason: str | None = None
    evidence_identity: str | None = None


@dataclass(frozen=True)
class TerminalFact:
    continuation_id: str | None
    directory: Path
    state: dict[str, Any]
    output: dict[str, Any]


@dataclass(frozen=True)
class ActiveTailFact:
    continuation_id: str
    directory: Path
    state: dict[str, Any]


@dataclass(frozen=True)
class TrustedContext:
    repository: Path | None = None
    evidence_roots: tuple[Path, ...] = ()
    related_work_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RunSnapshot:
    run_root: Path
    selected_terminal: TerminalFact | None
    latest_sealed_terminal: TerminalFact | None
    active_tail: ActiveTailFact | None
    invocation_identities: tuple[dict[str, Any], ...]
    recorded_outcomes: tuple[str, ...]
    frozen_work: dict[str, Any] | None
    routing: dict[str, Any] | None
    repository_identity: dict[str, Any] | None
    candidate_commit: str | None
    evidence_identities: dict[str, str]
    proof: ProofResult


_Reader = EvidenceReader


def _context(value, run_root):
    if isinstance(value, TrustedContext):
        context = value
    elif isinstance(value, dict):
        roots = value.get("evidence_roots", value.get("permitted_evidence_roots", ()))
        related = value.get(
            "related_work_roots", value.get("permitted_related_work_roots", ())
        )
        repository = value.get("repository") or value.get("repository_path")
        if isinstance(repository, dict):
            repository = repository.get("path") or repository.get("worktree")
        context = TrustedContext(
            Path(repository) if repository else None,
            tuple(Path(item) for item in roots),
            tuple(Path(item) for item in related),
        )
    else:
        raise TypeError("trusted_context must be TrustedContext or a mapping")
    roots = context.evidence_roots or (Path(run_root),)
    return context, tuple(roots) + tuple(context.related_work_roots)


def _terminal(identifier, directory, state, output):
    return TerminalFact(identifier, directory, state, output)


def read_run(run_root, selection="latest", trusted_context=None) -> RunSnapshot:
    """Read and verify one terminal prepared or standalone Coordinator Run.

    The complete retained continuation chain is always read before a historical
    terminal is selected.  The function performs no writes and no network or
    inference actions.
    """
    root = Path(run_root).absolute()
    context, roots = _context(trusted_context or {}, root)
    reader = _Reader(roots)
    proof = ProofResult("verified")
    selected = latest = None
    active = None
    assignment = request = preparation = None
    coordinator = root
    try:
        preparation_path = root / "preparation.json"
        try:
            preparation = reader.json(preparation_path)
        except _Unavailable as unavailable:
            # A missing preparation record selects the supported standalone
            # layout.  An existing record that cannot be proof-read (for
            # example because it is oversized) is still prepared-Run evidence
            # and must make the proof unavailable rather than silently
            # changing the layout being validated.
            if unavailable.reason != "missing evidence":
                raise
            preparation = None
        if preparation is not None:
            if (
                not isinstance(preparation, dict)
                or preparation.get("schema_version") != 1
            ):
                raise RunValidationError("invalid Run preparation evidence")
            coordinator = root / "coordinator"
        assignment = validate_assignment(reader.json(coordinator / "assignment.json"))
        request = validate_request(reader.json(coordinator / "input.json"))
        if preparation is not None:
            _validate_preparation(preparation, root, assignment, request, context)
        if assignment.get("related_work") != request.get("related_work"):
            raise RunValidationError("Coordinator related-work references disagree")
        related_reference = assignment.get("related_work")
        if related_reference is not None:
            related_path = related_reference.get("path")
            if (
                not isinstance(related_path, str)
                or not Path(related_path).is_absolute()
            ):
                raise RunValidationError("malformed related-work reference")
            related_raw = reader.bytes(Path(related_path), MAX_RELATED_WORK_BYTES)
            validate_snapshot_bytes(related_raw, related_reference)
        state = validate_checkpoint(reader.json(coordinator / "state.json"))
        if state["status"] == "running":
            raise RunValidationError("base Coordinator Run is not terminal")
        output = validate_output(reader.json(coordinator / "output.json"))
        require_terminal_pair(state, output)
        latest = _terminal(None, coordinator, state, output)
        terminals = {None: latest}

        def exhaustion_verifiers(invocation_roots):
            def invocation_directory(record):
                return _invocation_path(invocation_roots, record, "output.json").parent

            def verify_failed_validation(record):
                validate_repairable_failure(
                    invocation_directory(record),
                    reader=reader,
                    log_limit=MAX_VALIDATION_LOG_BYTES,
                )

            def verify_iteration(record):
                from .iteration import validate_sealed_result

                invocation = invocation_directory(record)
                validate_sealed_result(
                    reader.json(invocation / "input.json"),
                    reader.json(invocation / "output.json"),
                    reader=reader,
                    repository=context.repository,
                    verify_git=context.repository is not None,
                )

            return verify_failed_validation, verify_iteration

        observed = observe_lineage(
            coordinator,
            state,
            output,
            request["max_responses"],
            read_json=reader.json,
            locate_component=_invocation_path,
            exhaustion_verifiers=exhaustion_verifiers,
            allow_running=True,
            defer_error=lambda error: isinstance(error, _Unavailable),
        )
        directories = list(observed.directories)
        for item in observed.sealed:
            latest = _terminal(
                item.directory.name, item.directory, item.state, item.output
            )
            terminals[item.directory.name] = latest
        if observed.active is not None:
            item = observed.active
            active = ActiveTailFact(item.directory.name, item.directory, item.state)
        if selection == "latest":
            selected = latest
        else:
            key = str(selection)
            if key.startswith("continuation."):
                key = key.rsplit(".", 1)[-1]
            selected = terminals.get(key)
            if selected is None:
                raise RunValidationError(
                    "selected continuation is not a sealed terminal"
                )

        # Historical selection changes only the returned prefix.  Proof always
        # covers the newest retained history, including an active tail.
        proof_roots = [coordinator, *directories]
        proof_state = active.state if active is not None else latest.state
        unavailable_proof = (
            observed.deferred_errors[0] if observed.deferred_errors else None
        )
        for record in proof_state["history"]:
            if record["outcome"] == "abandoned":
                continue
            try:
                component_output = reader.json(
                    _invocation_path(proof_roots, record, "output.json")
                )
                outcome = validate_component_output(
                    record["component"], component_output
                )
                if outcome != record["outcome"]:
                    raise RunValidationError(
                        "component outcome disagrees with Coordinator history"
                    )
            except _Unavailable as unavailable:
                # Unavailable proof has lower precedence than corruption. Keep
                # checking every retained invocation so an earlier missing or
                # oversized artifact cannot conceal malformed later evidence.
                if unavailable_proof is None:
                    unavailable_proof = unavailable
        try:
            _verify_stage_provenance(
                reader, proof_state["history"], proof_roots, context, assignment
            )
        except _Unavailable as unavailable:
            if unavailable_proof is None:
                unavailable_proof = unavailable
        if unavailable_proof is not None:
            proof = ProofResult(
                "unavailable",
                unavailable_proof.reason,
                unavailable_proof.identity,
            )
    except _Unavailable as unavailable:
        proof = ProofResult("unavailable", unavailable.reason, unavailable.identity)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, RunValidationError):
            raise
        raise RunValidationError(str(error)) from error

    history = (
        selected.state["history"]
        if selected
        else (latest.state["history"] if latest else [])
    )
    candidate = (
        _candidate_commit(reader, selected, context, proof, coordinator)
        if selected
        else None
    )
    if isinstance(candidate, tuple):
        candidate, unavailable = candidate
        if proof.status == "verified":
            proof = unavailable
    repository_identity = _repository_identity(context.repository)
    frozen_work = assignment
    routing = preparation.get("routing") if isinstance(preparation, dict) else None
    return RunSnapshot(
        root,
        selected,
        latest,
        active,
        tuple(
            {
                "sequence": row["sequence"],
                "component": row["component"],
                "directory": row["directory"],
            }
            for row in history
        ),
        tuple(row["outcome"] for row in history),
        frozen_work,
        routing,
        repository_identity,
        candidate,
        dict(reader.identities),
        proof,
    )


def _validate_preparation(value, root, assignment, request, context):
    """Validate and bind prepared-Run routing and repository metadata."""
    required = {
        "schema_version",
        "run",
        "bead",
        "project",
        "related_work",
        "repository",
        "timestamps",
        "preparation_status",
        "routing",
        "coordinator",
        "errors",
    }
    if set(value) != required or value.get("preparation_status") != "prepared":
        raise RunValidationError("invalid prepared Run metadata")
    run = value.get("run")
    bead = value.get("bead")
    project = value.get("project")
    repository = value.get("repository")
    timestamps = value.get("timestamps")
    if (
        not isinstance(run, dict)
        or set(run) != {"id", "artifact_root"}
        or run.get("id") != root.name
        or Path(run.get("artifact_root", "")).absolute() != root
        or not isinstance(bead, dict)
        or set(bead) != {"id"}
        or not isinstance(bead.get("id"), str)
        or not bead["id"]
        or not isinstance(project, dict)
        or set(project) != {"slug"}
        or not isinstance(project.get("slug"), str)
        or not project["slug"]
        or not isinstance(repository, dict)
        or set(repository) != {"path", "base_ref", "base_commit", "branch", "worktree"}
        or any(
            not isinstance(repository.get(key), str) or not repository[key]
            for key in repository
        )
        or Path(repository["worktree"]).resolve()
        != Path(assignment["workspace"]).resolve()
        or not isinstance(timestamps, dict)
        or set(timestamps) != {"started_at", "prepared_at", "finished_at"}
        or not isinstance(timestamps.get("started_at"), str)
        or not isinstance(timestamps.get("prepared_at"), str)
        or timestamps.get("finished_at") is not None
        and not isinstance(timestamps.get("finished_at"), str)
        or "work_base" in assignment
        and assignment["work_base"] != repository["base_commit"]
        or value.get("related_work") != assignment.get("related_work")
        or value.get("related_work") != request.get("related_work")
        or not isinstance(value.get("errors"), list)
    ):
        raise RunValidationError("prepared Run metadata is not bound to this Run")
    if context.repository is not None and (
        Path(repository["path"]).resolve() != context.repository.resolve()
    ):
        raise RunValidationError(
            "prepared Run repository does not match trusted context"
        )
    routing = value.get("routing")
    if not isinstance(routing, dict) or set(routing) != {"planner", "policy"}:
        raise RunValidationError("invalid prepared Run routing")
    for name, result in (
        ("planner", "planner/output.json"),
        ("policy", "policy/output.json"),
    ):
        stage = routing.get(name)
        required_stage = {
            "command",
            "directory",
            "result",
            "status",
            "exit_code",
            "outcome",
        }
        if name == "policy":
            required_stage.add("decision")
        if (
            not isinstance(stage, dict)
            or set(stage) != required_stage
            or stage.get("directory") != name
            or stage.get("result") != result
            or not isinstance(stage.get("command"), list)
            or not stage["command"]
            or not all(isinstance(item, str) for item in stage["command"])
            or stage.get("status")
            not in {"not_started", "running", "completed", "failed"}
            or not (
                stage.get("exit_code") is None
                or isinstance(stage.get("exit_code"), int)
                and not isinstance(stage.get("exit_code"), bool)
            )
            or stage.get("outcome") not in {None, "completed", "failed"}
        ):
            raise RunValidationError("invalid prepared Run routing")
    coordinator = value.get("coordinator")
    if (
        not isinstance(coordinator, dict)
        or set(coordinator)
        != {
            "command",
            "directory",
            "result",
            "status",
            "exit_code",
            "outcome",
            "decision",
        }
        or coordinator.get("directory") != "coordinator"
        or coordinator.get("result") != "coordinator/output.json"
        or not isinstance(coordinator.get("command"), list)
        or not coordinator["command"]
        or not all(isinstance(item, str) for item in coordinator["command"])
        or coordinator.get("status")
        not in {"not_started", "running", "completed", "failed"}
        or not (
            coordinator.get("exit_code") is None
            or isinstance(coordinator.get("exit_code"), int)
            and not isinstance(coordinator.get("exit_code"), bool)
        )
        or coordinator.get("outcome") not in {None, "completed", "failed"}
        or coordinator.get("decision") not in {None, "stop", "exhausted"}
    ):
        raise RunValidationError("invalid prepared Run coordinator routing")


def _invocation_path(bases, record, name):
    for base in reversed(tuple(bases)):
        candidate = base / record["directory"] / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return Path(bases[0]) / record["directory"] / name


def _verify_stage_provenance(reader, history, roots, context, assignment):
    """Deeply prove every completed cycle, not merely its final rows.

    Availability failures are accumulated so they cannot conceal corruption in
    a later successful stage. Any structural contradiction still fails the
    complete snapshot immediately.
    """
    from .stages import verify_change_lineage, verify_source

    successful = []
    reviews = {}
    unavailable_proof = None
    for row in history:
        if row["outcome"] != COMPONENT_TOPOLOGY[row["component"]]["success"]:
            continue
        successful.append(row)
        directory = _invocation_path(roots, row, "output.json").parent
        try:
            if row["component"] == "change":
                lineage = verify_change_lineage(
                    directory,
                    reader=reader,
                    repository=context.repository,
                    verify_git=context.repository is not None,
                )
                _require_run_assignment(lineage.assignment, assignment, "Change")
            elif row["component"] == "validation":
                # A successful Validation is authoritative even while it is the
                # active tail, before a Review exists to consume it.
                source_row = next(
                    (
                        item
                        for item in reversed(successful[:-1])
                        if item["component"] in {"attempt", "response"}
                    ),
                    None,
                )
                if source_row is None:
                    raise RunValidationError(
                        "Validation lacks committed source provenance"
                    )
                source = verify_source(
                    source_row["component"],
                    _invocation_path(roots, source_row, "output.json").parent,
                    reader=reader,
                    repository=context.repository,
                    verify_git=context.repository is not None,
                )
                _require_run_assignment(source.assignment, assignment, "Validation")
                validation_input, validation_output, _stdout, _stderr = (
                    load_passed_evidence(
                        directory,
                        reader=reader,
                        log_limit=MAX_VALIDATION_LOG_BYTES,
                    )
                )
                if (
                    Path(validation_input["workspace"]).absolute()
                    != Path(assignment["workspace"]).absolute()
                    or _subject(validation_output["repository"]["before"])
                    != _subject(source.after)
                    or _subject(validation_output["repository"]["after"])
                    != _subject(source.after)
                ):
                    raise RunValidationError(
                        "Validation subject does not match its committed source"
                    )
            elif row["component"] == "review":
                change_row = next(
                    (
                        item
                        for item in reversed(successful[:-1])
                        if item["component"] == "change"
                    ),
                    None,
                )
                validation_row = next(
                    (
                        item
                        for item in reversed(successful[:-1])
                        if item["component"] == "validation"
                    ),
                    None,
                )
                if change_row is None or validation_row is None:
                    raise RunValidationError(
                        "Review lacks Change or Validation provenance"
                    )
                reviews[row["sequence"]] = _verify_review(
                    reader,
                    roots,
                    row,
                    change_row,
                    validation_row,
                    assignment,
                    context.repository,
                )
            elif row["component"] == "assessment":
                review_row = next(
                    (
                        item
                        for item in reversed(successful[:-1])
                        if item["component"] == "review"
                    ),
                    None,
                )
                if review_row is None:
                    raise RunValidationError("Assessment lacks Review provenance")
                review_facts = reviews.get(review_row["sequence"])
                if review_facts is None:
                    # The Review's proof may have been unavailable. Re-check it
                    # rather than converting that condition into a KeyError.
                    change_row = next(
                        item
                        for item in reversed(successful)
                        if item["component"] == "change"
                    )
                    validation_row = next(
                        item
                        for item in reversed(successful)
                        if item["component"] == "validation"
                    )
                    review_facts = _review_local_facts(
                        reader,
                        roots,
                        review_row,
                        change_row,
                        validation_row,
                        assignment,
                    )
                _verify_assessment(reader, roots, row, review_row, review_facts)
        except _Unavailable as unavailable:
            if unavailable_proof is None:
                unavailable_proof = unavailable
    if unavailable_proof is not None:
        raise unavailable_proof


def _require_run_assignment(stage_assignment, run_assignment, stage):
    if stage_assignment != run_assignment:
        raise RunValidationError(f"{stage} provenance does not match Assignment")


def _related_ids(reader, reference):
    if reference is None:
        return set()
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise RunValidationError("malformed related-work reference")
    raw = reader.bytes(Path(reference["path"]), MAX_RELATED_WORK_BYTES)
    validate_snapshot_bytes(raw, reference)
    return {json.loads(line)["id"] for line in raw.splitlines()}


def _review_local_facts(
    reader, roots, review_row, change_row, validation_row, assignment, repository=None
):
    """Validate Review facts that do not depend on Validation log access."""
    review_dir = _invocation_path(roots, review_row, "output.json").parent
    change_dir = _invocation_path(roots, change_row, "output.json").parent
    validation_dir = _invocation_path(roots, validation_row, "output.json").parent
    review_input = validate_review_input(reader.json(review_dir / "input.json"))
    review_output = reader.json(review_dir / "output.json")
    for field, expected in (
        ("change_directory", change_dir),
        ("validation_directory", validation_dir),
    ):
        value = review_input.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise RunValidationError(f"invalid Review {field}")
        reader.relative(value)
        if Path(value).absolute() != expected.absolute():
            raise RunValidationError(f"Review {field} does not match history")
    change = validate_change_output(reader.json(change_dir / "output.json"))
    repository_evidence = review_output.get("repository")
    if not isinstance(repository_evidence, dict):
        raise RunValidationError("invalid Review repository evidence")
    subject = _subject(change["repository"]["after"])
    review_states = [
        _subject(repository_evidence.get(key)) for key in ("before", "after")
    ]
    workspace = review_input.get("workspace")
    # Check facts local to Change and Review before reading transitive
    # Validation proof. Missing Validation logs must not hide a contradictory
    # later Review subject or workspace.
    if (
        review_output.get("outcome") != "completed"
        or repository_evidence.get("unchanged") is not True
        or any(state != subject for state in review_states)
    ):
        raise RunValidationError("Change and Review subjects disagree")
    if (
        not isinstance(workspace, str)
        or Path(workspace).absolute() != Path(change["workspace"]).absolute()
    ):
        raise RunValidationError("stage workspaces disagree")
    from afk_review.context import validate_retained_context

    validate_retained_context(
        review_dir, review_input, review_output, assignment, change, reader, repository
    )
    related = review_input.get("related_work")
    if assignment.get("related_work") != related:
        raise RunValidationError("stage related-work evidence must match Assignment")
    review = validate_review(
        review_output.get("review"),
        Path(workspace),
        subject["head"],
        _related_ids(reader, related),
    )
    return review_dir, review_input, review_output, review, subject


def _verify_review(
    reader, roots, review_row, change_row, validation_row, assignment, repository=None
):
    facts = _review_local_facts(
        reader, roots, review_row, change_row, validation_row, assignment, repository
    )
    _review_dir, review_input, review_output, _review, subject = facts
    validation_dir = _invocation_path(roots, validation_row, "output.json").parent
    validation_input, validation_output, validation_stdout, validation_stderr = (
        load_passed_evidence(
            validation_dir,
            reader=reader,
            log_limit=MAX_VALIDATION_LOG_BYTES,
        )
    )
    if review_output.get("validation_evidence") != evidence_identity(
        validation_input, validation_output, validation_stdout, validation_stderr
    ):
        raise RunValidationError("Review-bound Validation evidence identity disagrees")
    validation_states = [
        _subject(validation_output["repository"][key]) for key in ("before", "after")
    ]
    if any(state != subject for state in validation_states):
        raise RunValidationError("Change, Validation and Review subjects disagree")
    if (
        Path(validation_input["workspace"]).absolute()
        != Path(review_input["workspace"]).absolute()
    ):
        raise RunValidationError("stage workspaces disagree")
    return facts


def _verify_assessment(reader, roots, row, review_row, review_facts):
    review_dir, review_input, _review_output, review, subject = review_facts
    directory = _invocation_path(roots, row, "output.json").parent
    input_value = validate_assessment_input(reader.json(directory / "input.json"))
    output = reader.json(directory / "output.json")
    reference = input_value.get("review_directory")
    repository = output.get("repository")
    if (
        not isinstance(reference, str)
        or Path(reference).absolute() != review_dir.absolute()
        or not isinstance(repository, dict)
        or output.get("outcome") != "completed"
        or repository.get("unchanged") is not True
        or _subject(repository.get("before")) != subject
        or _subject(repository.get("after")) != subject
        or Path(input_value["workspace"]).absolute()
        != Path(review_input["workspace"]).absolute()
    ):
        raise RunValidationError("Assessment subject does not match Review")
    if input_value.get("related_work") != review_input.get("related_work"):
        raise RunValidationError(
            "Assessment related-work evidence disagrees with Review"
        )
    validate_assessment(
        review,
        output.get("assessment"),
        _related_ids(reader, input_value.get("related_work")),
    )


def _subject(value):
    state = validate_repository_state(value)
    return {field: state[field] for field in ("head", "dirty", "status")}


def _candidate_commit(reader, terminal, context, current_proof, coordinator):
    change = next(
        (
            row
            for row in reversed(terminal.state["history"])
            if row["component"] == "change" and row["outcome"] == "completed"
        ),
        None,
    )
    if change is None:
        return None
    try:
        continuation_root = coordinator / "continuations"
        roots = [coordinator]
        if terminal.continuation_id:
            roots.extend(
                continuation_root / f"{number:02d}"
                for number in range(1, int(terminal.continuation_id) + 1)
            )
        output = reader.json(_invocation_path(roots, change, "output.json"))
        committed_change = validate_change_output(output)
        commit = committed_change["repository"]["after"]["head"]
        if not isinstance(commit, str) or not commit:
            raise RunValidationError("invalid candidate commit")
    except _Unavailable as unavailable:
        return None, ProofResult(
            "unavailable", unavailable.reason, unavailable.identity
        )
    if context.repository is None:
        return commit, ProofResult(
            "unavailable", "local repository is not configured", commit
        )
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(context.repository),
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode:
        return commit, ProofResult(
            "unavailable", "candidate Git object is unavailable", commit
        )
    canonical = subprocess.run(
        ["git", "-C", str(context.repository), "rev-parse", f"{commit}^{{commit}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout.strip()
    if canonical != commit:
        raise RunValidationError("candidate commit is not canonical")
    return commit


def _repository_identity(repository):
    if repository is None:
        return None
    completed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode:
        return None
    return {"path": completed.stdout.strip()}
