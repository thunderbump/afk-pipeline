"""PROTOTYPE: make the independent Review file contract tangible."""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from contract import validate_review


def run(input_path: Path, result_directory: Path) -> dict[str, object]:
    review_input = json.loads(input_path.read_text())
    validate_input(review_input)
    evidence = load_evidence(review_input)
    verify_subject(review_input, evidence)

    result_directory.mkdir()
    write_json(result_directory / "input.json", review_input)
    events_path = result_directory / "events.jsonl"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()
    timed_out = False

    with events_path.open("wb") as events, stderr_path.open("wb") as stderr:
        try:
            completed = subprocess.run(
                pi_command(prompt(review_input, evidence)),
                cwd=review_input["workspace"],
                stdin=subprocess.DEVNULL,
                stdout=events,
                stderr=stderr,
                timeout=review_input["timeout_seconds"],
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None

    parsed_review = None
    error = None
    if not timed_out and exit_code == 0:
        try:
            parsed_review = validate_review(json.loads(final_text(events_path)))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as parse_error:
            error = str(parse_error)

    outcome = "timed_out" if timed_out else "completed" if parsed_review else "failed"
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "process": {"exit_code": exit_code},
        "review": parsed_review,
        **({"error": error} if error else {}),
        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
    }
    write_json(result_directory / "output.json", output)
    return output


def validate_input(value: object) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("review input must use schema_version 1")
    for field in ("workspace", "attempt_directory", "validation_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"{field} must be an absolute path")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive integer")


def load_evidence(review_input: dict[str, object]) -> dict[str, object]:
    attempt = Path(review_input["attempt_directory"])
    validation = Path(review_input["validation_directory"])
    return {
        "assignment": read_json(attempt / "input.json"),
        "attempt": read_json(attempt / "output.json"),
        "validation": read_json(validation / "output.json"),
    }


def verify_subject(
    review_input: dict[str, object], evidence: dict[str, object]
) -> None:
    attempt = evidence["attempt"]
    validation = evidence["validation"]
    if attempt.get("outcome") != "succeeded":
        raise ValueError("attempt must have succeeded")
    if validation.get("outcome") != "passed":
        raise ValueError("validation must have passed")
    reviewed_head = attempt["repository"]["after"]["head"]
    validated_head = validation["repository"]["after"]["head"]
    workspace_head = git(Path(review_input["workspace"]), "rev-parse", "HEAD")
    if reviewed_head != validated_head or reviewed_head != workspace_head:
        raise ValueError("workspace, attempt, and validation must identify one HEAD")


def prompt(review_input: dict[str, object], evidence: dict[str, object]) -> str:
    attempt = evidence["attempt"]
    assignment = evidence["assignment"]
    before = attempt["repository"]["before"]["head"]
    after = attempt["repository"]["after"]["head"]
    return f"""Review the implementation described below. Do not modify the workspace.

Objective: {assignment["objective"]}
Review exactly: git diff {before}..{after}
Attempt evidence: {review_input["attempt_directory"]}
Validation evidence: {review_input["validation_directory"]}

Look for concrete correctness defects, regressions, missing necessary tests, and violations of the stated objective. Validation passing is evidence, not proof of correctness. Do not propose or perform repairs.

Return only one JSON object with this exact shape:
{{
  "summary": "concise scope and conclusion",
  "findings": [
    {{
      "severity": "high|medium|low",
      "title": "concise problem",
      "details": "why it matters and when it occurs",
      "locations": [{{"path": "relative/file.py", "line": 1}}]
    }}
  ]
}}

Use an empty findings array when you find no actionable problem. Do not wrap the JSON in Markdown."""


def pi_command(user_prompt: str) -> list[str]:
    return [
        "/usr/bin/env",
        "PI_TELEMETRY=0",
        "PI_SKIP_VERSION_CHECK=1",
        "/home/bump/.local/bin/pi",
        "--provider",
        "openai-codex",
        "--model",
        "gpt-5.6-sol",
        "--thinking",
        "medium",
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--tools",
        "read,bash,grep,find,ls",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--system-prompt",
        "You are a read-only implementation reviewer. Inspect only the prepared workspace and the two named evidence directories. Do not modify files, create commits, access Beads, or change Git configuration. Your response must satisfy the JSON contract in the user prompt.",
        user_prompt,
    ]


def final_text(events_path: Path) -> str:
    terminal = None
    for line in events_path.read_bytes().decode("utf-8").splitlines():
        event = json.loads(line)
        if (
            event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ):
            terminal = event["message"]
    if terminal is None or terminal.get("stopReason") != "stop":
        raise ValueError("reviewer event stream did not complete")
    return "".join(
        part["text"]
        for part in terminal.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


def demo() -> None:
    states = {
        "n": {
            "outcome": "completed",
            "review": {"summary": "No actionable defects found.", "findings": []},
        },
        "f": {
            "outcome": "completed",
            "review": {
                "summary": "One actionable defect found.",
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Help exits with the wrong status",
                        "details": "The public help path returns 2 instead of 0.",
                        "locations": [{"path": "afk_validate/__main__.py", "line": 17}],
                    }
                ],
            },
        },
        "e": {
            "outcome": "failed",
            "review": None,
            "error": "response was not valid Review JSON",
        },
    }
    state = {"message": "Choose a terminal shape."}
    while True:
        render(state)
        choice = (
            input(
                "\n[n] no findings  [f] findings  [e] execution failure  [q] quit\n> "
            )
            .strip()
            .lower()
        )
        if choice == "q":
            return
        state = states.get(choice, {"message": f"Unknown action: {choice!r}"})


def render(state: dict[str, object]) -> None:
    print("\033[2J\033[H", end="")
    print("\033[1mPROTOTYPE — independent Review contract\033[0m")
    print("\033[2mReviewer completion and findings are separate facts.\033[0m\n")
    print(json.dumps(state, indent=2))


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=workspace, check=True, text=True, capture_output=True
    ).stdout.strip()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    elif len(sys.argv) == 3:
        print(json.dumps(run(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
    else:
        raise SystemExit("usage: prototype.py [REVIEW_JSON NEW_REVIEW_DIRECTORY]")
