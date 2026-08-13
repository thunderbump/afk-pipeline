import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


USAGE = "usage: python3 -m afk_validate VALIDATION_JSON RESULT_DIRECTORY"


def main() -> int:
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    validation = json.loads(input_path.read_text())
    validate(validation)
    workspace = Path(validation["workspace"])
    before = repository_state(workspace)

    result_directory.mkdir()
    write_json(result_directory / "input.json", validation)
    stdout_path = result_directory / "stdout.log"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    runner_error = None

    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                validation["command"],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as error:
            exit_code = None
            runner_error = str(error)
        else:
            try:
                exit_code = process.wait(timeout=validation["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = terminate(process)
            except KeyboardInterrupt:
                interrupted = True
                exit_code = terminate(process)

    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        after = None
        observation_error = str(error)
    head_changed = None if after is None else before["head"] != after["head"]
    outcome = (
        "interrupted"
        if interrupted
        else "timed_out"
        if timed_out
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
    temporary = result_directory / "output.json.tmp"
    write_json(temporary, output)
    os.replace(temporary, result_directory / "output.json")
    return 0 if outcome == "passed" else 1


def validate(validation: object) -> None:
    if not isinstance(validation, dict) or validation.get("schema_version") != 1:
        raise ValueError("validation must use schema_version 1")
    workspace = validation.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("validation workspace must be an absolute path")
    command = validation.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(argument, str) for argument in command
    ):
        raise ValueError("validation command must be a non-empty argv string array")
    timeout = validation.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("validation timeout_seconds must be a positive integer")


def repository_state(workspace: Path) -> dict[str, object]:
    head = git(workspace, "rev-parse", "HEAD")
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    status = git(workspace, "status", "--porcelain").splitlines()
    return {
        "head": head,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "dirty": bool(status),
        "status": status,
    }


def process_result(exit_code: int | None, error: str | None) -> dict[str, object]:
    result = {
        "exit_code": exit_code if exit_code is None or exit_code >= 0 else None,
        "signal": signal.Signals(-exit_code).name if exit_code is not None and exit_code < 0 else None,
    }
    if error:
        result["error"] = error
    return result


def terminate(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.wait()
    try:
        return process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait()


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=workspace, check=True, text=True, capture_output=True
    ).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"afk-validate: {error}", file=sys.stderr)
        raise SystemExit(2)
