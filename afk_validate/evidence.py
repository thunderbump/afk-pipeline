"""Validate a failed Validation that is safe to hand to a repair worker."""

import json
import math
import stat
from pathlib import Path

from afk_change.contract import validate_repository_state


def validate_repairable_failure(
    validation_directory: Path,
    workspace: Path | None = None,
    repository: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return sealed input/output for one ordinary, stable nonzero failure.

    Timeouts, interruptions, launch/observation errors, repository changes, dirty
    states, and unavailable logs are deliberately not repairable evidence.
    """
    validation_directory = Path(validation_directory)
    if validation_directory.is_symlink() or not validation_directory.is_dir():
        raise ValueError("Validation evidence directory is unavailable")
    validation_directory = validation_directory.resolve()
    validation_input = _read_object(validation_directory / "input.json", "input")
    validation_output = _read_object(validation_directory / "output.json", "output")
    if validation_input.get("schema_version") != 1:
        raise ValueError("Validation input must use schema_version 1")
    input_workspace = validation_input.get("workspace")
    command = validation_input.get("command")
    timeout = validation_input.get("timeout_seconds")
    if (
        not isinstance(input_workspace, str)
        or not Path(input_workspace).is_absolute()
        or not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) for argument in command)
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise ValueError("invalid failed Validation input")
    if workspace is not None and Path(input_workspace).resolve() != workspace.resolve():
        raise ValueError("workspace must match failed Validation evidence")

    required = {
        "schema_version",
        "outcome",
        "started_at",
        "finished_at",
        "duration_seconds",
        "process",
        "repository",
        "artifacts",
    }
    if (
        set(validation_output) != required
        or validation_output.get("schema_version") != 1
        or validation_output.get("outcome") != "failed"
        or not isinstance(validation_output.get("started_at"), str)
        or not validation_output["started_at"]
        or not isinstance(validation_output.get("finished_at"), str)
        or not validation_output["finished_at"]
        or not isinstance(validation_output.get("duration_seconds"), (int, float))
        or isinstance(validation_output.get("duration_seconds"), bool)
        or not math.isfinite(validation_output["duration_seconds"])
        or validation_output["duration_seconds"] < 0
    ):
        raise ValueError("invalid failed Validation output")
    process = validation_output.get("process")
    if (
        not isinstance(process, dict)
        or set(process) != {"exit_code", "signal"}
        or not isinstance(process.get("exit_code"), int)
        or isinstance(process.get("exit_code"), bool)
        or process["exit_code"] <= 0
        or process.get("signal") is not None
    ):
        raise ValueError("failed Validation was not an ordinary nonzero result")

    recorded_repository = validation_output.get("repository")
    if (
        not isinstance(recorded_repository, dict)
        or set(recorded_repository) != {"before", "after", "head_changed"}
        or recorded_repository.get("head_changed") is not False
    ):
        raise ValueError("failed Validation repository evidence is unstable")
    before = validate_repository_state(recorded_repository.get("before"))
    after = validate_repository_state(recorded_repository.get("after"))
    if before != after or before["dirty"] or before["status"]:
        raise ValueError("failed Validation changed the repository")
    if repository is not None and repository != after:
        raise ValueError("workspace drifted after failed Validation")

    artifacts = validation_output.get("artifacts")
    if artifacts != {"stdout": "stdout.log", "stderr": "stderr.log"}:
        raise ValueError("failed Validation logs are not identified")
    for name in artifacts.values():
        path = validation_directory / name
        try:
            facts = path.lstat()
        except OSError as error:
            raise ValueError("failed Validation logs are unavailable") from error
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError("failed Validation logs are unavailable")
    return validation_input, validation_output


def _read_object(path: Path, name: str) -> dict[str, object]:
    try:
        facts = path.lstat()
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"failed Validation {name} is unavailable")
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"failed Validation {name} is unavailable") from error
    if not isinstance(value, dict):
        raise TypeError(f"failed Validation {name} must be an object")
    return value
