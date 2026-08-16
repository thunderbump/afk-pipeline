"""Shared process and evidence mechanics for AFK executable modules."""

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run_command(
    command: list[str],
    workspace: Path,
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    timed_out = False
    interrupted = False
    error = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as launch_error:
            exit_code = None
            error = str(launch_error)
        else:
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = terminate(process)
            except KeyboardInterrupt:
                interrupted = True
                exit_code = terminate(process)
    return {
        "exit_code": exit_code,
        "error": error,
        "timed_out": timed_out,
        "interrupted": interrupted,
    }


def repository_state(workspace: Path) -> dict[str, object]:
    head = git(workspace, "rev-parse", "HEAD")
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
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
        workspace, "rev-list", "--reverse", f"{before['head']}..{after['head']}"
    ).splitlines()


def process_result(exit_code: int | None, error: str | None) -> dict[str, object]:
    result: dict[str, object] = {
        "exit_code": exit_code if exit_code is None or exit_code >= 0 else None,
        "signal": (
            signal.Signals(-exit_code).name
            if exit_code is not None and exit_code < 0
            else None
        ),
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


def seal_json(path: Path, value: object) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    write_json(temporary, value)
    os.replace(temporary, path)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def progress(message: str) -> None:
    """Emit best-effort wrapper progress without making sealing depend on stdout."""
    try:
        print(f"{timestamp()} {message}", flush=True)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
        sys.stdout = os.fdopen(os.open(os.devnull, os.O_WRONLY), "w")
