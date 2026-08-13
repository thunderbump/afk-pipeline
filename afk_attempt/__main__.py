import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from datetime import datetime, timezone


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python3 -m afk_attempt ASSIGNMENT_JSON ATTEMPT_DIRECTORY", file=sys.stderr)
        return 2

    assignment_path = Path(sys.argv[1])
    attempt_directory = Path(sys.argv[2])
    assignment = json.loads(assignment_path.read_text())
    validate_assignment(assignment)
    workspace = Path(assignment["workspace"])

    before = repository_state(workspace)
    attempt_directory.mkdir()
    write_json(attempt_directory / "input.json", assignment)
    events_path = attempt_directory / "events.jsonl"
    stderr_path = attempt_directory / "stderr.log"
    started_at = timestamp()

    with events_path.open("wb") as events, stderr_path.open("wb") as stderr:
        timed_out = False
        interrupted = False
        runner_error = None
        try:
            process = subprocess.Popen(
                assignment["command"],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=events,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as error:
            exit_code = None
            runner_error = str(error)
        else:
            try:
                exit_code = process.wait(timeout=assignment["timeout_seconds"])
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
    commits = None
    if after is not None:
        try:
            commits = commits_between_heads(workspace, before, after)
        except (OSError, subprocess.SubprocessError) as error:
            observation_error = str(error)
    agent = None if runner_error else agent_result(events_path)
    outcome = (
        "interrupted"
        if interrupted
        else "timed_out"
        if timed_out
        else "succeeded"
        if (
            exit_code == 0
            and agent is not None
            and agent["status"] == "completed"
            and observation_error is None
        )
        else "failed"
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "process": process_result(exit_code, runner_error),
        "agent": agent,
        "repository": {
            "before": before,
            "after": after,
            "commits_between_heads": commits,
            **({"observation_error": observation_error} if observation_error else {}),
        },
        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
    }
    temporary = attempt_directory / "output.json.tmp"
    write_json(temporary, output)
    os.replace(temporary, attempt_directory / "output.json")
    return 0 if outcome == "succeeded" else 1


def validate_assignment(assignment: object) -> None:
    if not isinstance(assignment, dict) or assignment.get("schema_version") != 1:
        raise ValueError("assignment must use schema_version 1")
    if not isinstance(assignment.get("objective"), str) or not assignment["objective"].strip():
        raise ValueError("assignment objective must be a non-empty string")
    workspace = assignment.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("assignment workspace must be an absolute path")
    command = assignment.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("assignment command must be a non-empty argv string array")
    timeout = assignment.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("assignment timeout_seconds must be a positive integer")


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


def commits_between_heads(
    workspace: Path, before: dict[str, object], after: dict[str, object] | None
) -> list[str] | None:
    if after is None:
        return None
    if before["head"] == after["head"]:
        return []
    return git(
        workspace, "rev-list", "--reverse", f'{before["head"]}..{after["head"]}'
    ).splitlines()


def agent_result(events_path: Path) -> dict[str, str]:
    saw_end = False
    terminal_message = None
    try:
        lines = events_path.read_bytes().decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return {"status": "error", "error": "invalid agent event encoding"}
    for line in lines:
        if saw_end:
            return {"status": "error", "error": "events follow agent_end"}
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return {"status": "error", "error": "invalid agent event JSON"}
        if not isinstance(event, dict):
            return {"status": "error", "error": "invalid agent event JSON"}
        if event.get("type") == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                return {"status": "error", "error": "invalid agent event JSON"}
            if message.get("role") == "assistant":
                terminal_message = message
        if event.get("type") == "agent_end":
            saw_end = True
    if not saw_end or terminal_message is None:
        return {"status": "error", "error": "agent event stream did not complete"}
    if terminal_message.get("stopReason") == "error":
        return {"status": "error", "error": terminal_message.get("errorMessage", "agent error")}
    if terminal_message.get("stopReason") == "aborted":
        return {"status": "aborted"}
    return {"status": "completed"}


def process_result(exit_code: int | None, error: str | None) -> dict[str, object]:
    result: dict[str, object] = {
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


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, check=True, text=True, capture_output=True
    ).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"afk-attempt: {error}", file=sys.stderr)
        raise SystemExit(2)
