import json
import os
import subprocess
import sys
import time
from pathlib import Path

from afk_agent import agent_response
from afk_runtime import (
    git,
    process_result,
    progress,
    repository_state,
    run_command,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_review REVIEW_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Run one AFK review from REVIEW_JSON and seal its artifacts in RESULT_DIRECTORY.

Arguments:
  REVIEW_JSON       Path to the review JSON file.
  RESULT_DIRECTORY  New directory where review input, output, and logs are written.
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
    progress("loading review input")
    review_input = json.loads(input_path.read_text())
    validate_input(review_input)
    command_prefix = agent_command()
    progress("review input accepted")

    progress("loading Attempt and Validation evidence")
    evidence = load_evidence(review_input)
    workspace = Path(review_input["workspace"])
    progress("observing reviewed repository")
    before = repository_state(workspace)
    verify_subject(before, evidence)

    progress("preparing review result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", review_input)
    diff_path = result_directory / "diff.patch"
    write_diff(diff_path, workspace, evidence)
    events_path = result_directory / "events.jsonl"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()
    progress(
        "starting review agent "
        f"(timeout={review_input['timeout_seconds']}s; "
        f"artifacts: events={events_path}, stderr={stderr_path})"
    )
    execution = run_command(
        [*command_prefix, prompt(review_input, evidence, diff_path)],
        workspace,
        review_input["timeout_seconds"],
        events_path,
        stderr_path,
    )
    progress("review agent completed")

    progress("observing repository after review")
    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        after = None
        observation_error = str(error)
    unchanged = None if after is None else before == after

    response = None if execution["error"] else agent_response(events_path)
    agent = None if response is None else response["agent"]
    review = None
    review_error = None
    if agent is not None and agent["status"] == "completed":
        try:
            reviewed_head = evidence["attempt"]["repository"]["after"]["head"]
            review = validate_review(
                json.loads(response["text"]), workspace, reviewed_head
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            review_error = str(error)

    outcome = (
        "interrupted"
        if execution["interrupted"]
        else "timed_out"
        if execution["timed_out"]
        else "completed"
        if (
            execution["exit_code"] == 0
            and agent is not None
            and agent["status"] == "completed"
            and review is not None
            and unchanged is True
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
        "process": process_result(execution["exit_code"], execution["error"]),
        "agent": agent,
        "review": review,
        **({"review_error": review_error} if review_error else {}),
        "repository": {
            "before": before,
            "after": after,
            "unchanged": unchanged,
            **({"observation_error": observation_error} if observation_error else {}),
        },
        "artifacts": {
            "diff": "diff.patch",
            "events": "events.jsonl",
            "stderr": "stderr.log",
        },
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} review outcome at {output_path}")
    return 0 if outcome == "completed" else 1


def validate_input(value: object) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("review must use schema_version 1")
    for field in ("workspace", "attempt_directory", "validation_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"review {field} must be an absolute path")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("review timeout_seconds must be a positive integer")


def agent_command() -> list[str]:
    configured = os.environ.get("AFK_REVIEW_AGENT_COMMAND")
    if configured is not None:
        command = json.loads(configured)
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) for argument in command)
        ):
            raise ValueError("AFK_REVIEW_AGENT_COMMAND must be a JSON argv array")
        return command
    return [
        "/usr/bin/env",
        "PI_TELEMETRY=0",
        "PI_SKIP_VERSION_CHECK=1",
        "pi",
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
        "read,grep,find,ls",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--system-prompt",
        "You are a read-only implementation reviewer. Inspect only the prepared workspace and the named diff and evidence paths. Your response must satisfy the JSON contract in the user prompt.",
    ]


def load_evidence(review_input: dict[str, object]) -> dict[str, object]:
    attempt = Path(review_input["attempt_directory"])
    validation = Path(review_input["validation_directory"])
    return {
        "assignment": read_json(attempt / "input.json"),
        "attempt": read_json(attempt / "output.json"),
        "validation": read_json(validation / "output.json"),
    }


def verify_subject(before: dict[str, object], evidence: dict[str, object]) -> None:
    try:
        assignment = evidence["assignment"]
        attempt = evidence["attempt"]
        validation = evidence["validation"]
        objective = assignment["objective"]
        attempt_state = subject_state(attempt["repository"]["after"])
        validation_state = subject_state(validation["repository"]["after"])
        base_head = attempt["repository"]["before"]["head"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid Review evidence") from error
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("invalid Review evidence objective")
    if not isinstance(base_head, str) or not base_head:
        raise ValueError("invalid Review evidence base HEAD")
    if attempt.get("outcome") != "succeeded":
        raise ValueError("review Attempt must have succeeded")
    if validation.get("outcome") != "passed":
        raise ValueError("review Validation must have passed")
    if attempt_state != validation_state:
        raise ValueError("Attempt and Validation must identify one repository state")
    if subject_state(before) != attempt_state:
        raise ValueError("workspace must match the validated Attempt repository state")
    if attempt_state["dirty"] or attempt_state["status"]:
        raise ValueError("Review requires a clean committed state")


def subject_state(state: dict[str, object]) -> dict[str, object]:
    return {field: state[field] for field in ("head", "dirty", "status")}


def validate_review(
    value: object, workspace: Path, reviewed_head: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("review response must be an object")
    if not isinstance(value.get("summary"), str):
        raise TypeError("review summary must be a string")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise TypeError("review findings must be an array")
    for finding in findings:
        validate_finding(finding, workspace, reviewed_head)
    return value


def validate_finding(finding: object, workspace: Path, reviewed_head: str) -> None:
    if not isinstance(finding, dict):
        raise TypeError("each finding must be an object")
    if finding.get("severity") not in {"high", "medium", "low"}:
        raise ValueError("finding severity must be high, medium, or low")
    for field in ("title", "details"):
        if not isinstance(finding.get(field), str):
            raise TypeError(f"finding {field} must be a string")
        if not finding[field].strip():
            raise ValueError(f"finding {field} must be a non-empty string")
    locations = finding.get("locations")
    if not isinstance(locations, list):
        raise TypeError("finding locations must be an array")
    if not locations:
        raise ValueError("finding locations must not be empty")
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            raise TypeError("each finding location needs a path")
        if not location["path"].strip() or location["path"].startswith("/"):
            raise ValueError("finding location path must be repository-relative")
        line = location.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            raise TypeError("finding location line must be an integer")
        if line < 1:
            raise ValueError("finding location line must be a positive integer")
        validate_location(workspace, reviewed_head, location["path"], line)


def validate_location(
    workspace: Path, reviewed_head: str, path: str, line: int
) -> None:
    root = workspace.resolve()
    target = (workspace / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("finding location path escapes the repository") from error
    blob = subprocess.run(
        ["git", "show", f"{reviewed_head}:{path}"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0:
        raise ValueError("finding location path must name a reviewed file")
    try:
        line_count = len(blob.stdout.decode("utf-8").splitlines())
    except UnicodeDecodeError as error:
        raise ValueError("finding location path must name a text file") from error
    if line > line_count:
        raise ValueError("finding location line must exist in the reviewed file")


def write_diff(diff_path: Path, workspace: Path, evidence: dict[str, object]) -> None:
    attempt = evidence["attempt"]
    before = attempt["repository"]["before"]["head"]
    after = attempt["repository"]["after"]["head"]
    diff = git(
        workspace,
        "diff",
        "--no-ext-diff",
        "--binary",
        f"{before}..{after}",
        "--",
    )
    diff_path.write_text(diff + ("\n" if diff else ""))


def prompt(
    review_input: dict[str, object],
    evidence: dict[str, object],
    diff_path: Path,
) -> str:
    attempt = evidence["attempt"]
    assignment = evidence["assignment"]
    before = attempt["repository"]["before"]["head"]
    after = attempt["repository"]["after"]["head"]
    return f"""Review the implementation described below. Do not modify the workspace.

Objective: {assignment["objective"]}
Reviewed commits: {before}..{after}
Read the complete reviewed diff from: {diff_path}
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

Every finding must have at least one location. Each location uses a repository-relative path and a positive 1-based line number in the reviewed HEAD. Use an empty findings array when you find no actionable problem. Do not wrap the JSON in Markdown."""


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"afk-review: {error}", file=sys.stderr)
        raise SystemExit(2)
