"""Pure structural verification for retained Coordinator continuations.

This module deliberately has no dependency on executable modules.  Both the
Coordinator and observers use these functions, so a continuation cannot acquire
a different meaning depending on who reads it.
"""

from __future__ import annotations

from pathlib import Path

from afk_coordinate.contract import validate_continuation


def continuation_directories(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
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
    ):
        raise ValueError("continuation lineage does not match its predecessor")


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


def require_exhausted_structure(state, expected_max_responses, read_component):
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
    policy = output.get("policy") if isinstance(output, dict) else None
    if (
        output.get("outcome") != "completed"
        or not isinstance(policy, dict)
        or policy.get("decision") != "exhausted"
        or policy.get("max_responses") != expected_max_responses
        or policy.get("completed_responses") != completed
    ):
        raise ValueError("exhausted continuation requires matching Iteration evidence")
