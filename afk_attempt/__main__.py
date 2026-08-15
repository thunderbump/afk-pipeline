import json
import os
import subprocess
import sys
from pathlib import Path

from afk_runtime import (
    git,
    process_result,
    repository_state,
    run_command,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_attempt ASSIGNMENT_JSON ATTEMPT_DIRECTORY"

HELP = f"""{USAGE}

Run one AFK attempt from ASSIGNMENT_JSON and seal its artifacts in ATTEMPT_DIRECTORY.

Arguments:
  ASSIGNMENT_JSON    Path to the assignment JSON file.
  ATTEMPT_DIRECTORY  New directory where attempt input, output, and logs are written.
"""


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    assignment_path = Path(sys.argv[1])
    attempt_directory = Path(sys.argv[2])
    progress("loading assignment input")
    assignment = json.loads(assignment_path.read_text())
    validate_assignment(assignment)
    progress("assignment input accepted")
    workspace = Path(assignment["workspace"])

    progress("observing repository before attempt")
    before = repository_state(workspace)
    progress("preparing attempt directory")
    attempt_directory.mkdir()
    write_json(attempt_directory / "input.json", assignment)
    events_path = attempt_directory / "events.jsonl"
    stderr_path = attempt_directory / "stderr.log"
    started_at = timestamp()

    progress(
        "starting agent child "
        f"(timeout={assignment['timeout_seconds']}s; "
        f"artifacts: events={events_path}, stderr={stderr_path})"
    )
    execution = run_command(
        assignment["command"],
        workspace,
        assignment["timeout_seconds"],
        events_path,
        stderr_path,
    )
    progress("agent child completed")
    exit_code = execution["exit_code"]
    runner_error = execution["error"]

    progress("observing repository after attempt")
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
        if execution["interrupted"]
        else "timed_out"
        if execution["timed_out"]
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
    output_path = attempt_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} attempt outcome at {output_path}")
    return 0 if outcome == "succeeded" else 1


def progress(message: str) -> None:
    try:
        print(f"{timestamp()} {message}", flush=True)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        sys.stdout = os.fdopen(os.open(os.devnull, os.O_WRONLY), "w")


def validate_assignment(assignment: object) -> None:
    if not isinstance(assignment, dict) or assignment.get("schema_version") != 1:
        raise ValueError("assignment must use schema_version 1")
    if (
        not isinstance(assignment.get("objective"), str)
        or not assignment["objective"].strip()
    ):
        raise ValueError("assignment objective must be a non-empty string")
    workspace = assignment.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("assignment workspace must be an absolute path")
    command = assignment.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) for arg in command)
    ):
        raise ValueError("assignment command must be a non-empty argv string array")
    timeout = assignment.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("assignment timeout_seconds must be a positive integer")


def commits_between_heads(
    workspace: Path, before: dict[str, object], after: dict[str, object] | None
) -> list[str] | None:
    if after is None:
        return None
    if before["head"] == after["head"]:
        return []
    return git(
        workspace, "rev-list", "--reverse", f"{before['head']}..{after['head']}"
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
        return {
            "status": "error",
            "error": terminal_message.get("errorMessage", "agent error"),
        }
    if terminal_message.get("stopReason") == "aborted":
        return {"status": "aborted"}
    return {"status": "completed"}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-attempt: {error}", file=sys.stderr)
        raise SystemExit(2)
