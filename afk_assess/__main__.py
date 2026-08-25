import json
import subprocess
import sys
import time
from pathlib import Path

from afk_agent import agent_response, read_only_pi_command
from afk_assess.contract import subject_state, validate_assessment
from afk_change.contract import validate_change_output
from afk_config import INFERENCE_ROLE_DEFAULTS, validate_inference_setting
from afk_review.contract import validate_review
from afk_runtime import (
    process_result,
    progress,
    repository_state,
    run_command,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_assess ASSESSMENT_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Assess one completed AFK Review from ASSESSMENT_JSON and seal its artifacts in RESULT_DIRECTORY.

Arguments:
  ASSESSMENT_JSON  Path to the finding-assessment JSON file.
  RESULT_DIRECTORY New directory where assessment input, output, and logs are written.
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
    progress("loading finding-assessment input")
    assessment_input = json.loads(input_path.read_text())
    validate_input(assessment_input)
    inference = assessment_input.get(
        "inference", INFERENCE_ROLE_DEFAULTS["finding_assessment"]
    )
    command_prefix = read_only_pi_command(
        "AFK_ASSESS_AGENT_COMMAND",
        "You are a read-only finding assessor. Inspect only the prepared workspace and named Review evidence. Decide whether each reported finding is worth addressing. Do not modify files or prescribe a repair. Your response must satisfy the JSON contract in the user prompt.",
        inference["model"],
        inference["thinking"],
    )
    progress("finding-assessment input accepted")

    progress("loading completed Review evidence")
    evidence = load_evidence(assessment_input)
    workspace = Path(assessment_input["workspace"])
    progress("observing reviewed repository")
    before = repository_state(workspace)
    review, objective = verify_subject(assessment_input, before, evidence)

    progress("preparing finding-assessment result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", assessment_input)
    events_path = result_directory / "events.jsonl"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()
    progress(
        "starting finding-assessment agent "
        f"(timeout={assessment_input['timeout_seconds']}s; "
        f"artifacts: events={events_path}, stderr={stderr_path})"
    )
    execution = run_command(
        [*command_prefix, prompt(assessment_input, review, objective)],
        workspace,
        assessment_input["timeout_seconds"],
        events_path,
        stderr_path,
    )
    progress("finding-assessment agent completed")

    progress("observing repository after finding assessment")
    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        after = None
        observation_error = str(error)
    unchanged = None if after is None else before == after

    response = None if execution["error"] else agent_response(events_path)
    agent = None if response is None else response["agent"]
    assessment = None
    assessment_error = None
    if agent is not None and agent["status"] == "completed":
        try:
            assessment = validate_assessment(review, json.loads(response["text"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            assessment_error = str(error)

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
            and assessment is not None
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
        "assessment": assessment,
        **({"assessment_error": assessment_error} if assessment_error else {}),
        "repository": {
            "before": before,
            "after": after,
            "unchanged": unchanged,
            **({"observation_error": observation_error} if observation_error else {}),
        },
        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} finding-assessment outcome at {output_path}")
    return 0 if outcome == "completed" else 1


def validate_input(value: object) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("finding assessment must use schema_version 1")
    for field in ("workspace", "review_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"finding assessment {field} must be an absolute path")
    if "inference" in value:
        validate_inference_setting(value["inference"])
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(
            "finding assessment timeout_seconds must be a positive integer"
        )


def load_evidence(assessment_input: dict[str, object]) -> dict[str, object]:
    review_directory = Path(assessment_input["review_directory"])
    review_input = read_json(review_directory / "input.json")
    if not isinstance(review_input, dict):
        raise TypeError("invalid Review evidence")
    change_directory = review_input.get("change_directory")
    if (
        not isinstance(change_directory, str)
        or not Path(change_directory).is_absolute()
    ):
        raise ValueError("invalid Review evidence change_directory")
    return {
        "input": review_input,
        "output": read_json(review_directory / "output.json"),
        "change_output": read_json(Path(change_directory) / "output.json"),
    }


def verify_subject(
    assessment_input: dict[str, object],
    before: dict[str, object],
    evidence: dict[str, object],
) -> tuple[dict[str, object], str]:
    try:
        review_input = evidence["input"]
        review_output = evidence["output"]
        change = validate_change_output(evidence["change_output"])
        review_workspace = review_input["workspace"]
        change_workspace = change["workspace"]
        change_state = subject_state(change["repository"]["after"])
        review_before = subject_state(review_output["repository"]["before"])
        review_state = subject_state(review_output["repository"]["after"])
        reviewed_head = review_state["head"]
        review = review_output["review"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid Review evidence") from error
    objective = change["objective"]
    if review_output.get("outcome") != "completed":
        raise ValueError("finding assessment requires a completed Review")
    if review_output["repository"].get("unchanged") is not True:
        raise ValueError("completed Review must identify an unchanged repository")
    if review_before != review_state:
        raise ValueError("completed Review must identify an unchanged repository")
    if (
        not isinstance(review_workspace, str)
        or not Path(review_workspace).is_absolute()
    ):
        raise ValueError("invalid Review evidence")
    if (
        Path(review_workspace).resolve()
        != Path(assessment_input["workspace"]).resolve()
    ):
        raise ValueError("workspace must match the completed Review input")
    if Path(change_workspace).resolve() != Path(review_workspace).resolve():
        raise ValueError("workspace must match the reviewed Committed Change")
    if change_state != review_state:
        raise ValueError("Review must match its Committed Change repository state")
    if subject_state(before) != review_state:
        raise ValueError("workspace must match the completed Review repository state")
    if review_state["dirty"] or review_state["status"]:
        raise ValueError("finding assessment requires a clean committed state")
    return (
        validate_review(review, Path(assessment_input["workspace"]), reviewed_head),
        objective,
    )


def prompt(
    assessment_input: dict[str, object], review: dict[str, object], objective: str
) -> str:
    review_directory = Path(assessment_input["review_directory"])
    return f"""Assess whether every finding in this completed Review is worth addressing. Do not modify the workspace and do not prescribe repairs.

Review findings:
{json.dumps(review["findings"], indent=2)}

Implementation objective: {objective}
Review evidence: {review_directory}
Reviewed diff: {review_directory / "diff.patch"}

For each finding, inspect the reviewed code and evidence. Mark worth_addressing true only when the reported problem is concrete, reachable, and relevant to the implementation objective. Use the immutable zero-based array position as finding_index.

Return only one JSON object with this exact shape:
{{
  "summary": "concise assessment conclusion",
  "decisions": [
    {{
      "finding_index": 0,
      "worth_addressing": true,
      "rationale": "why the finding is or is not worth addressing"
    }}
  ]
}}

Return exactly one decision for every finding, with no duplicate or omitted indices. Use an empty decisions array when the Review has no findings. Do not wrap the JSON in Markdown."""


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
        print(f"afk-assess: {error}", file=sys.stderr)
        raise SystemExit(2)
