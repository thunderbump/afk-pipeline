import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from afk_assess.contract import subject_state, validate_assessment
from afk_change.contract import validate_change_output
from afk_change.evidence import verify_source
from afk_inference import invoke
from afk_related_work import snapshot_ids
from afk_respond.contract import actionable_findings, validate_input
from afk_respond.task import build_task
from afk_review.contract import validate_review
from afk_runtime import (
    commits_between_heads,
    process_result,
    progress,
    repository_state,
    seal_json,
    timestamp,
    write_json,
)
from afk_validate.evidence import validate_repairable_failure

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

    workspace = Path(response_input["workspace"])
    repair = "validation_directory" in response_input
    progress("observing response repository")
    before = repository_state(workspace)
    if repair:
        progress("loading failed Validation evidence")
        verify_validation_subject(response_input, before)
        selected = []
        objective = response_input["objective"]
    else:
        progress("loading Finding Assessment evidence")
        evidence = load_evidence(response_input)
        review, assessment, objective = verify_subject(response_input, before, evidence)
        selected = actionable_findings(review, assessment)
    requires_agent = repair or bool(selected)

    progress("preparing feedback-response result directory")
    result_directory.mkdir()
    write_json(result_directory / "input.json", response_input)
    events_path = result_directory / "events.jsonl"
    stderr_path = result_directory / "stderr.log"
    started_at = timestamp()
    started = time.monotonic()

    if not requires_agent:
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

    task = build_task(response_input, selected, objective)

    inference_result = invoke(
        purpose=task.purpose,
        task_contract_version=task.contract_version,
        trusted_task_instructions=task.trusted_instructions,
        untrusted_task_data=task.untrusted_data,
        requested_capability=task.capability,
        execution_root=workspace,
        timeout_seconds=response_input["timeout_seconds"],
        evidence_directory=result_directory / "inference",
        validator=task.validator,
    )
    publish_runtime_logs(result_directory, inference_result.receipt)
    progress("feedback-response agent completed")

    progress("observing repository after feedback response")
    after, commits, descends_from_before, observation_error = (
        observe_repository_transition(workspace, before)
    )

    response = (
        inference_result.value if inference_result.outcome == "succeeded" else None
    )
    validation = inference_result.receipt["validation"]
    response_error = (
        validation.get("error")
        if inference_result.outcome in {"response_rejected", "validator_failed"}
        else None
    )
    terminal = inference_result.receipt["terminal_response"]
    agent = (
        {"status": "completed"}
        if terminal is not None
        and inference_result.receipt["protocol"].get("status") == "accepted"
        else None
    )
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
        if inference_result.outcome == "interrupted"
        else "timed_out"
        if inference_result.outcome == "timed_out"
        else "completed"
        if inference_result.outcome == "succeeded" and valid_repository
        else "failed"
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "process": runtime_process(inference_result.receipt),
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


def verify_validation_subject(response_input, before):
    validation_directory = Path(response_input["validation_directory"])
    _validation_input, validation_output = validate_repairable_failure(
        validation_directory, Path(response_input["workspace"]), before
    )
    source = response_input["source"]
    lineage = verify_source(source["kind"], Path(source["directory"]))
    if (
        subject_state(lineage.after)
        != subject_state(validation_output["repository"]["before"])
        or Path(lineage.assignment["workspace"]).resolve()
        != Path(response_input["workspace"]).resolve()
        or lineage.assignment["objective"] != response_input["objective"]
    ):
        raise ValueError("validation repair source does not match failed Validation")


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
    review_related = review_input.get("related_work")
    if assessment_input.get("related_work") != review_related:
        raise ValueError("Finding Assessment must use the Review related-work snapshot")
    related_work_ids = (
        snapshot_ids(review_related) if review_related is not None else set()
    )
    reviewed = validate_review(
        review, workspace, assessment_state["head"], related_work_ids
    )
    assessed = validate_assessment(reviewed, assessment, related_work_ids)
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


def publish_runtime_logs(result: Path, receipt: object) -> None:
    attempts = receipt["attempts"]
    for artifact, filename in (("events", "events.jsonl"), ("stderr", "stderr.log")):
        source = attempts[-1]["artifacts"].get(artifact) if attempts else None
        if source:
            shutil.copyfile(result / "inference" / source, result / filename)
        else:
            (result / filename).touch()


def runtime_process(receipt: object) -> dict[str, object]:
    attempts = receipt["attempts"]
    process = attempts[-1].get("process", {}) if attempts else {}
    return process_result(process.get("exit_code"), process.get("error"))


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
