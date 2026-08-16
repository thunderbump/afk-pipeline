import json
import subprocess
import sys
import time
from pathlib import Path

from afk_agent import agent_response, write_pi_command
from afk_assess.contract import subject_state, validate_assessment
from afk_change.contract import validate_change_output
from afk_respond.contract import actionable_findings, validate_input, validate_response
from afk_review.contract import validate_review
from afk_runtime import (
    commits_between_heads,
    process_result,
    progress,
    repository_state,
    run_command,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_respond RESPONSE_JSON RESULT_DIRECTORY"

HELP = f"""{USAGE}

Respond to one completed AFK Finding Assessment and seal its artifacts in RESULT_DIRECTORY.

Arguments:
  RESPONSE_JSON     Path to the feedback-response JSON file.
  RESULT_DIRECTORY New directory where response input, output, and logs are written.
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
    progress("loading feedback-response input")
    response_input = json.loads(input_path.read_text())
    validate_input(response_input)
    progress("feedback-response input accepted")

    progress("loading Finding Assessment evidence")
    evidence = load_evidence(response_input)
    workspace = Path(response_input["workspace"])
    progress("observing assessed repository")
    before = repository_state(workspace)
    review, assessment, objective = verify_subject(response_input, before, evidence)
    selected = actionable_findings(review, assessment)
    command_prefix = None
    if selected:
        command_prefix = write_pi_command(
            "AFK_RESPOND_AGENT_COMMAND",
            "You are an implementation feedback responder. Modify only the prepared workspace to address the actionable assessed findings in the user prompt, create a clean Git commit, and return the required JSON response. Do not address dismissed findings, run external orchestration, or publish feedback.",
        )

    progress("preparing feedback-response result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", response_input)
    events_path = result_directory / "events.jsonl"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()

    if not selected:
        events_path.touch()
        stderr_path.touch()
        progress("observing repository after no-action feedback response")
        after, commits, descends_from_before, observation_error = (
            observe_repository_transition(workspace, before)
        )
        unchanged = after == before if after is not None else None
        outcome = (
            "completed" if unchanged is True and observation_error is None else "failed"
        )
        output = {
            "schema_version": 1,
            "outcome": outcome,
            "started_at": started_at,
            "finished_at": timestamp(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "process": None,
            "agent": None,
            "response": {
                "summary": "No assessed findings were worth addressing.",
                "finding_responses": [],
            },
            "repository": {
                "before": before,
                "after": after,
                "unchanged": unchanged,
                "commits_between_heads": commits,
                "descends_from_before": descends_from_before,
                **(
                    {"observation_error": observation_error}
                    if observation_error
                    else {}
                ),
            },
            "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
        }
        output_path = result_directory / "output.json"
        seal_json(output_path, output)
        progress(f"sealed {outcome} feedback-response outcome at {output_path}")
        return 0 if outcome == "completed" else 1

    progress(
        "starting feedback-response agent "
        f"(timeout={response_input['timeout_seconds']}s; "
        f"artifacts: events={events_path}, stderr={stderr_path})"
    )
    execution = run_command(
        [*command_prefix, prompt(response_input, selected, objective)],
        workspace,
        response_input["timeout_seconds"],
        events_path,
        stderr_path,
    )
    progress("feedback-response agent completed")

    progress("observing repository after feedback response")
    after, commits, descends_from_before, observation_error = (
        observe_repository_transition(workspace, before)
    )

    parsed = None if execution["error"] else agent_response(events_path)
    agent = None if parsed is None else parsed["agent"]
    response = None
    response_error = None
    if agent is not None and agent["status"] == "completed":
        try:
            response = validate_response(selected, json.loads(parsed["text"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            response_error = str(error)

    valid_repository = (
        after is not None
        and after["dirty"] is False
        and after["status"] == []
        and after["head"] != before["head"]
        and bool(commits)
        and descends_from_before is True
        and observation_error is None
    )
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
            and response is not None
            and valid_repository
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
        "response": response,
        **({"response_error": response_error} if response_error else {}),
        "repository": {
            "before": before,
            "after": after,
            "commits_between_heads": commits,
            "descends_from_before": descends_from_before,
            **({"observation_error": observation_error} if observation_error else {}),
        },
        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} feedback-response outcome at {output_path}")
    return 0 if outcome == "completed" else 1


def load_evidence(response_input: dict[str, object]) -> dict[str, object]:
    assessment_directory = Path(response_input["assessment_directory"])
    assessment_input = read_json(assessment_directory / "input.json")
    if not isinstance(assessment_input, dict):
        raise TypeError("invalid Feedback Response evidence")
    review_directory = assessment_input.get("review_directory")
    if (
        not isinstance(review_directory, str)
        or not Path(review_directory).is_absolute()
    ):
        raise ValueError("invalid Feedback Response evidence review_directory")
    review_input = read_json(Path(review_directory) / "input.json")
    if not isinstance(review_input, dict):
        raise TypeError("invalid Feedback Response evidence")
    change_directory = review_input.get("change_directory")
    if (
        not isinstance(change_directory, str)
        or not Path(change_directory).is_absolute()
    ):
        raise ValueError("invalid Feedback Response evidence change_directory")
    return {
        "assessment_input": assessment_input,
        "assessment_output": read_json(assessment_directory / "output.json"),
        "review_input": review_input,
        "review_output": read_json(Path(review_directory) / "output.json"),
        "change_output": read_json(Path(change_directory) / "output.json"),
    }


def verify_subject(response_input, before, evidence):
    try:
        assessment_input = evidence["assessment_input"]
        assessment_output = evidence["assessment_output"]
        review_input = evidence["review_input"]
        review_output = evidence["review_output"]
        change = validate_change_output(evidence["change_output"])
        assessment_before = subject_state(assessment_output["repository"]["before"])
        assessment_state = subject_state(assessment_output["repository"]["after"])
        review_before = subject_state(review_output["repository"]["before"])
        review_state = subject_state(review_output["repository"]["after"])
        review = review_output["review"]
        assessment = assessment_output["assessment"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid Feedback Response evidence") from error
    objective = change["objective"]
    change_state = subject_state(change["repository"]["after"])
    if assessment_output.get("outcome") != "completed":
        raise ValueError("feedback response requires a completed Finding Assessment")
    if review_output.get("outcome") != "completed":
        raise ValueError("feedback response requires a completed Review")
    if assessment_output["repository"].get("unchanged") is not True:
        raise ValueError("completed Finding Assessment must be read-only")
    if review_output["repository"].get("unchanged") is not True:
        raise ValueError("completed Review must be read-only")
    if not (
        assessment_before
        == assessment_state
        == review_before
        == review_state
        == change_state
    ):
        raise ValueError("Review and Finding Assessment must identify one Git state")
    workspace = Path(response_input["workspace"]).resolve()
    for evidence_workspace in (
        change.get("workspace"),
        assessment_input.get("workspace"),
        review_input.get("workspace"),
    ):
        if (
            not isinstance(evidence_workspace, str)
            or Path(evidence_workspace).resolve() != workspace
        ):
            raise ValueError("workspace must match the Finding Assessment evidence")
    if subject_state(before) != assessment_state:
        raise ValueError("workspace must match the assessed repository state")
    if assessment_state["dirty"] or assessment_state["status"]:
        raise ValueError("feedback response requires a clean committed state")
    reviewed = validate_review(review, workspace, assessment_state["head"])
    assessed = validate_assessment(reviewed, assessment)
    return reviewed, assessed, objective


def head_descends_from(workspace, before, after):
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", before["head"], after["head"]],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise subprocess.CalledProcessError(completed.returncode, completed.args)
    return completed.returncode == 0 and before["head"] != after["head"]


def observe_repository_transition(workspace, before):
    after = None
    commits = None
    descends_from_before = None
    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        observation_error = str(error)
    if after is not None:
        try:
            commits = commits_between_heads(workspace, before, after)
            descends_from_before = head_descends_from(workspace, before, after)
        except (OSError, subprocess.SubprocessError) as error:
            observation_error = str(error)
    return after, commits, descends_from_before, observation_error


def prompt(response_input, selected, objective):
    assessment_directory = Path(response_input["assessment_directory"])
    return f"""Address every actionable assessed finding below in the prepared workspace and commit the result. Do not address findings omitted from this list.

Implementation objective: {objective}
Actionable findings:
{json.dumps(selected, indent=2)}

Finding Assessment evidence: {assessment_directory}
Finding Assessment input: {assessment_directory / "input.json"}

Return only one JSON object with this exact shape after creating a clean Git commit:
{{
  "summary": "concise description of the completed response",
  "finding_responses": [
    {{"finding_index": 0, "response": "what changed for this finding"}}
  ]
}}

Return exactly one response for every supplied finding_index, with no duplicate or omitted indices. Do not wrap the JSON in Markdown."""


def read_json(path):
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
        print(f"afk-respond: {error}", file=sys.stderr)
        raise SystemExit(2)
