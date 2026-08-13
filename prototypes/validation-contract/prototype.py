"""PROTOTYPE: make the repository Validation file contract tangible."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def run(input_path: Path, result_directory: Path) -> dict[str, object]:
    validation = json.loads(input_path.read_text())
    validate(validation)
    workspace = Path(validation["workspace"])
    before = repository_state(workspace)

    result_directory.mkdir()
    write_json(result_directory / "input.json", validation)
    started_at = timestamp()
    started = time.monotonic()
    timed_out = False

    with (result_directory / "stdout.log").open("wb") as stdout, (
        result_directory / "stderr.log"
    ).open("wb") as stderr:
        try:
            completed = subprocess.run(
                validation["command"],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=validation["timeout_seconds"],
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None

    after = repository_state(workspace)
    head_changed = before["head"] != after["head"]
    outcome = (
        "timed_out"
        if timed_out
        else "passed"
        if exit_code == 0 and not head_changed
        else "failed"
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "process": {"exit_code": exit_code},
        "repository": {
            "before": before,
            "after": after,
            "head_changed": head_changed,
        },
        "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
    }
    write_json(result_directory / "output.json", output)
    return output


def validate(validation: object) -> None:
    if not isinstance(validation, dict) or validation.get("schema_version") != 1:
        raise ValueError("validation must use schema_version 1")
    workspace = validation.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("workspace must be an absolute path")
    command = validation.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(argument, str) for argument in command
    ):
        raise ValueError("command must be a non-empty argv string array")
    timeout = validation.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive integer")


def repository_state(workspace: Path) -> dict[str, object]:
    status = git(workspace, "status", "--porcelain").splitlines()
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    return {
        "head": git(workspace, "rev-parse", "HEAD"),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "dirty": bool(status),
        "status": status,
    }


def demo() -> None:
    with tempfile.TemporaryDirectory(prefix="validation-contract-prototype-") as root:
        root_path = Path(root)
        action_number = 0
        last_state: dict[str, object] = {"message": "Choose a scenario."}

        while True:
            render(last_state)
            choice = input("\n[p] pass  [f] fail  [h] move HEAD  [q] quit\n> ").strip().lower()
            if choice == "q":
                return
            if choice not in {"p", "f", "h"}:
                last_state = {"message": f"Unknown action: {choice!r}"}
                continue

            action_number += 1
            case = root_path / f"case-{action_number}"
            workspace = prepare_workspace(case / "workspace")
            command = demo_command(choice)
            validation = {
                "schema_version": 1,
                "workspace": str(workspace),
                "command": command,
                "timeout_seconds": 5,
            }
            input_path = case / "validation.json"
            write_json(input_path, validation)
            result_directory = case / "result"
            output = run(input_path, result_directory)
            last_state = {
                "input": validation,
                "output": output,
                "stdout": (result_directory / "stdout.log").read_text(),
                "stderr": (result_directory / "stderr.log").read_text(),
            }


def prepare_workspace(workspace: Path) -> Path:
    workspace.mkdir(parents=True)
    git(workspace, "init", "--quiet", "--initial-branch", "main")
    git(workspace, "config", "user.name", "Validation Prototype")
    git(workspace, "config", "user.email", "validation@example.invalid")
    (workspace / "README.md").write_text("prototype workspace\n")
    git(workspace, "add", "README.md")
    git(workspace, "commit", "--quiet", "-m", "Initial state")
    return workspace


def demo_command(choice: str) -> list[str]:
    if choice == "p":
        return [sys.executable, "-c", "print('repository validation passed')"]
    if choice == "f":
        return [
            sys.executable,
            "-c",
            "import sys; print('repository validation failed', file=sys.stderr); sys.exit(7)",
        ]
    return [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import subprocess; "
            "Path('changed.txt').write_text('changed\\n'); "
            "subprocess.run(['git', 'add', 'changed.txt'], check=True); "
            "subprocess.run(['git', 'commit', '--quiet', '-m', 'Moved HEAD'], check=True); "
            "print('command passed after moving HEAD')"
        ),
    ]


def render(state: dict[str, object]) -> None:
    print("\033[2J\033[H", end="")
    print("\033[1mPROTOTYPE — deterministic repository Validation contract\033[0m")
    print("\033[2mDoes one exact command plus observed Git identity say enough?\033[0m\n")
    print(json.dumps(state, indent=2))


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    elif len(sys.argv) == 3:
        print(json.dumps(run(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
    else:
        raise SystemExit(
            "usage: prototype.py [VALIDATION_JSON NEW_RESULT_DIRECTORY]"
        )
