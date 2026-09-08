"""Read-only, independently revalidated local Run evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from afk_attempt.contract import validate_assignment
from afk_change.contract import validate_change_output, validate_repository_state
from afk_coordinate.contract import (
    COMPONENT_TOPOLOGY,
    validate_checkpoint,
    validate_component_output,
    validate_output,
    validate_request,
)
from afk_validate.evidence import evidence_identity

from .continuation import (
    continuation_directories,
    require_exhausted_structure,
    require_terminal_pair,
    validate_link,
)

MAX_JSON_BYTES = 1024 * 1024
MAX_RELATED_WORK_BYTES = 256 * 1024
MAX_VALIDATION_LOG_BYTES = 25 * 1024 * 1024


class RunValidationError(ValueError):
    """Evidence exists but is malformed, contradictory, or unsafe."""


class _Unavailable(Exception):
    def __init__(self, reason: str, identity: str):
        self.reason, self.identity = reason, identity


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


class _Reader:
    def __init__(self, roots):
        self.roots = tuple(self._safe_root(Path(root)) for root in roots)
        self.identities: dict[str, str] = {}

    @staticmethod
    def _safe_root(root):
        absolute = root.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                facts = current.lstat()
            except OSError as error:
                raise RunValidationError(
                    "trusted evidence root is unavailable"
                ) from error
            if stat.S_ISLNK(facts.st_mode):
                raise RunValidationError("trusted evidence root contains a symlink")
        if not absolute.is_dir():
            raise RunValidationError("trusted evidence root is not a directory")
        return absolute

    def _relative(self, path):
        absolute = Path(path).absolute()
        for root in self.roots:
            try:
                return root, absolute.relative_to(root)
            except ValueError:
                pass
        raise RunValidationError(
            f"evidence reference escapes trusted roots: {absolute}"
        )

    def bytes(self, path, limit, *, missing_unavailable=True):
        root, relative = self._relative(path)
        if not relative.parts:
            raise RunValidationError("evidence reference is not a file")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = [descriptor]
        try:
            for part in relative.parts[:-1]:
                if part in ("", ".", ".."):
                    raise RunValidationError("unsafe evidence path")
                descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                opened.append(descriptor)
            try:
                file_descriptor = os.open(
                    relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError as error:
                if missing_unavailable:
                    raise _Unavailable("missing evidence", str(path)) from error
                raise
            except OSError as error:
                raise RunValidationError(f"unsafe evidence file: {path}") from error
            opened.append(file_descriptor)
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise RunValidationError(f"evidence is not a regular file: {path}")
            if before.st_size > limit:
                raise _Unavailable("evidence exceeds proof-read limit", str(path))
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = os.read(file_descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(file_descriptor)
            if len(raw) > limit:
                raise _Unavailable("evidence exceeds proof-read limit", str(path))
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(raw) != before.st_size:
                raise RunValidationError("evidence changed while it was read")
            key = str(Path(path).absolute())
            self.identities[key] = hashlib.sha256(raw).hexdigest()
            return raw
        finally:
            for item in reversed(opened):
                os.close(item)

    def json(self, path):
        raw = self.bytes(path, MAX_JSON_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunValidationError(f"malformed JSON evidence: {path}") from error
        return value


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
        except _Unavailable:
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
            if (
                related_reference.get("bytes") != len(related_raw)
                or related_reference.get("sha256")
                != hashlib.sha256(related_raw).hexdigest()
            ):
                raise RunValidationError("related-work snapshot identity disagrees")
            try:
                related_records = [
                    json.loads(line)
                    for line in related_raw.decode("utf-8").splitlines()
                ]
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RunValidationError("malformed related-work snapshot") from error
            if (
                not related_records
                or not all(isinstance(record, dict) for record in related_records)
                or related_reference.get("record_count") != len(related_records)
            ):
                raise RunValidationError("related-work snapshot membership disagrees")
        state = validate_checkpoint(reader.json(coordinator / "state.json"))
        if state["status"] == "running":
            raise RunValidationError("base Coordinator Run is not terminal")
        output = validate_output(reader.json(coordinator / "output.json"))
        require_terminal_pair(state, output)
        latest = _terminal(None, coordinator, state, output)
        terminals = {None: latest}
        expected_limit = request["max_responses"]
        prior_output = "../../output.json"
        directories = continuation_directories(coordinator / "continuations")
        retained_roots = [coordinator]
        for index, directory in enumerate(directories):
            require_exhausted_structure(
                state,
                expected_limit,
                lambda record, name, bases=tuple(retained_roots): reader.json(
                    _invocation_path(bases, record, name)
                ),
            )
            continuation_input = reader.json(directory / "input.json")
            continuation_state = validate_checkpoint(
                reader.json(directory / "state.json")
            )
            validate_link(state, continuation_state, continuation_input, prior_output)
            if continuation_state["status"] == "running":
                if (
                    index != len(directories) - 1
                    or (directory / "output.json").exists()
                ):
                    raise RunValidationError(
                        "continuation lineage has work after an active tail"
                    )
                active = ActiveTailFact(directory.name, directory, continuation_state)
                break
            continuation_output = validate_output(
                reader.json(directory / "output.json")
            )
            require_terminal_pair(continuation_state, continuation_output)
            latest = _terminal(
                directory.name, directory, continuation_state, continuation_output
            )
            terminals[directory.name] = latest
            retained_roots.append(directory)
            state, output = continuation_state, continuation_output
            expected_limit = continuation_input["effective_max_responses"]
            prior_output = f"../{directory.name}/output.json"
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

        # Prove every selected invocation envelope and bind the bytes read.
        selected_roots = [
            coordinator,
            *directories[: int(selected.continuation_id or "0")],
        ]
        for record in selected.state["history"]:
            if record["outcome"] == "abandoned":
                continue
            component_output = reader.json(
                _invocation_path(selected_roots, record, "output.json")
            )
            outcome = validate_component_output(record["component"], component_output)
            if outcome != record["outcome"]:
                raise RunValidationError(
                    "component outcome disagrees with Coordinator history"
                )
        _verify_stage_provenance(reader, selected, selected_roots)
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


def _invocation_path(bases, record, name):
    for base in reversed(tuple(bases)):
        candidate = base / record["directory"] / name
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return Path(bases[0]) / record["directory"] / name


def _verify_stage_provenance(reader, terminal, roots):
    history = terminal.state["history"]
    by_component = {}
    for row in history:
        if row["outcome"] == COMPONENT_TOPOLOGY[row["component"]]["success"]:
            by_component[row["component"]] = row
    review_row = by_component.get("review")
    if review_row is None:
        return
    change_row = by_component.get("change")
    validation_row = by_component.get("validation")
    if change_row is None or validation_row is None:
        raise RunValidationError("Review lacks Change or Validation provenance")
    review_dir = _invocation_path(roots, review_row, "output.json").parent
    change_dir = _invocation_path(roots, change_row, "output.json").parent
    validation_dir = _invocation_path(roots, validation_row, "output.json").parent
    review_input = reader.json(review_dir / "input.json")
    review_output = reader.json(review_dir / "output.json")
    expected_paths = {
        "change_directory": change_dir.absolute(),
        "validation_directory": validation_dir.absolute(),
    }
    for field, expected in expected_paths.items():
        value = review_input.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise RunValidationError(f"invalid Review {field}")
        reader._relative(Path(value))
        if Path(value).absolute() != expected:
            raise RunValidationError(f"Review {field} does not match history")

    change = validate_change_output(reader.json(change_dir / "output.json"))
    validation_input = reader.json(validation_dir / "input.json")
    validation_output = reader.json(validation_dir / "output.json")
    _validate_passed_validation(validation_input, validation_output)
    logs = []
    for name in ("stdout.log", "stderr.log"):
        raw = reader.bytes(validation_dir / name, MAX_VALIDATION_LOG_BYTES)
        try:
            logs.append(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise RunValidationError("Validation log is not UTF-8") from error
    identity = evidence_identity(validation_input, validation_output, logs[0], logs[1])
    if review_output.get("validation_evidence") != identity:
        raise RunValidationError("Review-bound Validation evidence identity disagrees")

    change_state = _subject(change["repository"]["after"])
    validation_before = _subject(validation_output["repository"]["before"])
    validation_after = _subject(validation_output["repository"]["after"])
    review_repository = review_output.get("repository")
    if not isinstance(review_repository, dict):
        raise RunValidationError("invalid Review repository evidence")
    review_before = _subject(review_repository.get("before"))
    review_after = _subject(review_repository.get("after"))
    if (
        review_output.get("outcome") != "completed"
        or review_repository.get("unchanged") is not True
        or not (
            change_state
            == validation_before
            == validation_after
            == review_before
            == review_after
        )
    ):
        raise RunValidationError("Change, Validation and Review subjects disagree")
    workspace = review_input.get("workspace")
    validation_workspace = validation_input.get("workspace")
    if (
        not isinstance(workspace, str)
        or not isinstance(validation_workspace, str)
        or Path(workspace).absolute() != Path(change["workspace"]).absolute()
        or Path(validation_workspace).absolute() != Path(workspace).absolute()
    ):
        raise RunValidationError("stage workspaces disagree")

    assessment_row = by_component.get("assessment")
    if assessment_row:
        assessment_dir = _invocation_path(roots, assessment_row, "output.json").parent
        assessment_input = reader.json(assessment_dir / "input.json")
        assessment_output = reader.json(assessment_dir / "output.json")
        review_reference = assessment_input.get("review_directory")
        repository = assessment_output.get("repository")
        if (
            not isinstance(review_reference, str)
            or Path(review_reference).absolute() != review_dir.absolute()
            or not isinstance(repository, dict)
            or assessment_output.get("outcome") != "completed"
            or repository.get("unchanged") is not True
            or _subject(repository.get("before")) != review_after
            or _subject(repository.get("after")) != review_after
        ):
            raise RunValidationError("Assessment subject does not match Review")
        if assessment_input.get("related_work") != review_input.get("related_work"):
            raise RunValidationError(
                "Assessment related-work evidence disagrees with Review"
            )


def _subject(value):
    state = validate_repository_state(value)
    return {field: state[field] for field in ("head", "dirty", "status")}


def _validate_passed_validation(validation_input, validation_output):
    if (
        not isinstance(validation_input, dict)
        or validation_input.get("schema_version") != 1
        or not isinstance(validation_input.get("workspace"), str)
        or not Path(validation_input["workspace"]).is_absolute()
        or not isinstance(validation_input.get("command"), list)
        or not validation_input["command"]
        or not all(isinstance(item, str) for item in validation_input["command"])
        or not isinstance(validation_input.get("timeout_seconds"), int)
        or isinstance(validation_input.get("timeout_seconds"), bool)
        or validation_input["timeout_seconds"] <= 0
    ):
        raise RunValidationError("invalid passed Validation input")
    repository = (
        validation_output.get("repository")
        if isinstance(validation_output, dict)
        else None
    )
    if (
        validation_output.get("schema_version") != 1
        or validation_output.get("outcome") != "passed"
        or validation_output.get("process") != {"exit_code": 0, "signal": None}
        or validation_output.get("artifacts")
        != {"stdout": "stdout.log", "stderr": "stderr.log"}
        or not isinstance(repository, dict)
        or repository.get("head_changed") is not False
    ):
        raise RunValidationError("invalid passed Validation output")
    before = _subject(repository.get("before"))
    after = _subject(repository.get("after"))
    if before != after:
        raise RunValidationError("passed Validation changed repository content")


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
