import json
import subprocess
import sys
import time
from pathlib import Path

from afk_runtime import (
    process_result,
    repository_state,
    run_command,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_validate VALIDATION_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Run one AFK validation from VALIDATION_JSON and seal its artifacts in RESULT_DIRECTORY.

Arguments:
  VALIDATION_JSON   Path to the validation JSON file.
  RESULT_DIRECTORY  New directory where validation input, output, and logs are written.
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
    progress("loading validation input")
    validation = json.loads(input_path.read_text())
    validate(validation)
    progress("validation input accepted")
    workspace = Path(validation["workspace"])
    progress("observing repository before validation")
    before = repository_state(workspace)

    progress("preparing validation result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", validation)
    stdout_path = result_directory / "stdout.log"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()
    progress("starting validation child")
    execution = run_command(
        validation["command"],
        workspace,
        validation["timeout_seconds"],
        stdout_path,
        stderr_path,
    )
    progress("validation child completed")
    exit_code = execution["exit_code"]
    runner_error = execution["error"]

    progress("observing repository after validation")
    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        after = None
        observation_error = str(error)
    head_changed = None if after is None else before["head"] != after["head"]
    outcome = (
        "interrupted"
        if execution["interrupted"]
        else "timed_out"
        if execution["timed_out"]
        else "passed"
        if (
            exit_code == 0
            and runner_error is None
            and head_changed is False
            and observation_error is None
        )
        else "failed"
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "process": process_result(exit_code, runner_error),
        "repository": {
            "before": before,
            "after": after,
            "head_changed": head_changed,
            **({"observation_error": observation_error} if observation_error else {}),
        },
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} validation outcome at {output_path}")
    return 0 if outcome == "passed" else 1


def progress(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def validate(validation: object) -> None:
    if not isinstance(validation, dict) or validation.get("schema_version") != 1:
        raise ValueError("validation must use schema_version 1")
    workspace = validation.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("validation workspace must be an absolute path")
    command = validation.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) for argument in command)
    ):
        raise ValueError("validation command must be a non-empty argv string array")
    timeout = validation.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("validation timeout_seconds must be a positive integer")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-validate: {error}", file=sys.stderr)
        raise SystemExit(2)
