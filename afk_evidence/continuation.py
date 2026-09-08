"""Pure structural verification for retained Coordinator continuations.

This module deliberately has no dependency on executable modules.  Both the
Coordinator and observers use these functions, so a continuation cannot acquire
a different meaning depending on who reads it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afk_coordinate.contract import (
    validate_checkpoint,
    validate_continuation,
    validate_output,
)


@dataclass(frozen=True)
class ObservedContinuation:
    directory: Path
    input: dict[str, Any]
    state: dict[str, Any]
    output: dict[str, Any] | None


@dataclass(frozen=True)
class ContinuationObservation:
    sealed: tuple[ObservedContinuation, ...]
    active: ObservedContinuation | None
    selected: ObservedContinuation | None
    directories: tuple[Path, ...]


def continuation_directories(root: Path) -> list[Path]:
    # Test the directory entry before existence: exists() follows links and is
    # false for a dangling symlink, which must not masquerade as no lineage.
    if root.is_symlink():
        raise ValueError("continuations must be a real directory")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("continuations must be a real directory")
    directories = sorted(root.iterdir())
    expected = [f"{number:02d}" for number in range(1, len(directories) + 1)]
    if any(
        item.name != name or item.is_symlink() or not item.is_dir()
        for item, name in zip(directories, expected, strict=True)
    ):
        raise ValueError("continuation directories are malformed")
    return directories


def validate_link(prior_state, continuation_state, continuation_input, prior_output):
    continuation_input = validate_continuation(continuation_input)
    prior_history = prior_state["history"]
    completed = sum(
        row["component"] == "response" and row["outcome"] == "completed"
        for row in prior_history
    )
    if (
        continuation_input["prior_output"] != prior_output
        or continuation_input["completed_responses"] != completed
        or continuation_state.get("continuation") != continuation_input
        or continuation_state["history"][: len(prior_history)] != prior_history
        or len(continuation_state["history"]) < len(prior_history)
        or continuation_state["next_sequence"] < prior_state["next_sequence"]
        # A sealed continuation is evidence of newly performed work, not a
        # second terminal opinion over an unchanged predecessor checkpoint.
        or continuation_state["status"] != "running"
        and (
            len(continuation_state["history"]) == len(prior_history)
            or continuation_state["next_sequence"] == prior_state["next_sequence"]
        )
    ):
        raise ValueError("continuation lineage does not match its predecessor")


def observe_lineage(
    coordinator: Path,
    initial_state,
    initial_output,
    initial_max_responses,
    *,
    read_json: Callable[[Path], dict[str, Any]],
    locate_component: Callable[[tuple[Path, ...], dict[str, Any], str], Path],
    exhaustion_verifiers=None,
    allow_running=True,
    terminal_continuation=None,
):
    """Observe, validate, and optionally select a complete continuation chain.

    Callers retain their own bounded reader and deep stage-proof policy while
    this function is the single owner of ordering, predecessor linkage,
    exhaustion arithmetic, active-tail rules, and historical selection.
    """
    directories = tuple(continuation_directories(coordinator / "continuations"))
    state = initial_state
    require_terminal_pair(initial_state, initial_output)
    expected_limit = initial_max_responses
    prior_output = "../../output.json"
    retained_roots = [coordinator]
    sealed = []
    active = None
    selected = None
    for index, directory in enumerate(directories):
        roots = tuple(retained_roots)
        failed_verifier = iteration_verifier = None
        if exhaustion_verifiers is not None:
            failed_verifier, iteration_verifier = exhaustion_verifiers(roots)
        require_exhausted_structure(
            state,
            expected_limit,
            lambda record, name, current_roots=roots: read_json(
                locate_component(current_roots, record, name)
            ),
            verify_failed_validation=failed_verifier,
            verify_iteration=iteration_verifier,
        )
        continuation_input = validate_continuation(read_json(directory / "input.json"))
        continuation_state = validate_checkpoint(read_json(directory / "state.json"))
        validate_link(state, continuation_state, continuation_input, prior_output)
        if continuation_state["status"] == "running":
            if (
                not allow_running
                or index != len(directories) - 1
                or (directory / "output.json").exists()
                or (directory / "output.json").is_symlink()
            ):
                raise ValueError("newest continuation is not terminal")
            active = ObservedContinuation(
                directory, continuation_input, continuation_state, None
            )
            break
        continuation_output = validate_output(read_json(directory / "output.json"))
        require_terminal_pair(continuation_state, continuation_output)
        item = ObservedContinuation(
            directory, continuation_input, continuation_state, continuation_output
        )
        sealed.append(item)
        if directory.name == terminal_continuation:
            selected = item
        retained_roots.append(directory)
        state = continuation_state
        expected_limit = continuation_input["effective_max_responses"]
        prior_output = f"../{directory.name}/output.json"
    if terminal_continuation is not None and selected is None:
        raise ValueError("selected continuation is not a sealed terminal")
    return ContinuationObservation(tuple(sealed), active, selected, directories)


def output_from_state(state):
    if state["status"] == "completed":
        return {
            "schema_version": 1,
            "outcome": "completed",
            "decision": state["terminal"]["decision"],
            "history": state["history"],
        }
    if state["status"] == "failed":
        return {
            "schema_version": 1,
            "outcome": "failed",
            **state["terminal"],
            "history": state["history"],
        }
    raise ValueError("running Coordinator checkpoint has no terminal output")


def require_terminal_pair(state, output):
    if output != output_from_state(state):
        raise ValueError("terminal output does not match coordinator checkpoint")


def require_exhausted_structure(
    state,
    expected_max_responses,
    read_component,
    *,
    verify_failed_validation=None,
    verify_iteration=None,
):
    """Prove the recorded reason for an exhausted terminal, without Git access.

    ``read_component(record, name)`` returns a parsed object and lets callers
    enforce their own bounded/no-follow read discipline.
    """
    if state["status"] != "completed" or state["terminal"] != {"decision": "exhausted"}:
        raise ValueError("only an exhausted Coordinator Run can be continued")
    history = state["history"]
    completed = sum(
        row["component"] == "response" and row["outcome"] == "completed"
        for row in history
    )
    # Validation repair exhaustion has no Iteration invocation.
    last_real = next(
        (row for row in reversed(history) if row["outcome"] != "abandoned"), None
    )
    if (
        last_real
        and last_real["component"] == "validation"
        and last_real["outcome"] == "failed"
    ):
        if completed != expected_max_responses:
            raise ValueError(
                "exhausted continuation requires matching Validation repair evidence"
            )
        output = read_component(last_real, "output.json")
        if output.get("outcome") != "failed":
            raise ValueError("Validation repair outcome disagrees with history")
        if verify_failed_validation is not None:
            verify_failed_validation(last_real)
        return
    iteration = next(
        (
            row
            for row in reversed(history)
            if row["component"] == "iteration" and row["outcome"] == "completed"
        ),
        None,
    )
    if iteration is None:
        raise ValueError("exhausted continuation lacks Iteration evidence")
    output = read_component(iteration, "output.json")
    if verify_iteration is not None:
        # This re-derives policy from the referenced Review/Assessment lineage;
        # copied policy fields alone are not continuation authority.
        verify_iteration(iteration)
    policy = output.get("policy") if isinstance(output, dict) else None
    if (
        output.get("outcome") != "completed"
        or not isinstance(policy, dict)
        or policy.get("decision") != "exhausted"
        or policy.get("max_responses") != expected_max_responses
        or policy.get("completed_responses") != completed
    ):
        raise ValueError("exhausted continuation requires matching Iteration evidence")
