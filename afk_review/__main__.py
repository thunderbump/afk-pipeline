import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from afk_change.contract import validate_change_output, validate_git_transition
from afk_config import INFERENCE_ROLE_DEFAULTS, validate_inference_setting
from afk_inference import Capability, ResponseRejected, invoke
from afk_related_work import validate_reference, validate_snapshot
from afk_review.contract import validate_review
from afk_runtime import (
    git,
    process_result,
    progress,
    repository_state,
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

REVIEW_INSTRUCTIONS = """Act as a read-only implementation reviewer. Audit the complete objective and acceptance criteria, reviewed diff, supplied Committed Change and Validation evidence, and relevant repository files. Look for concrete correctness defects, regressions, missing necessary tests, and violations of the objective. Validation passing is evidence, not proof. Do not modify files, propose repairs, or stop after the first defect.

Return only one JSON object with this exact shape and field order:
{"summary":"concise scope and conclusion","findings":[{"severity":"high|medium|low","title":"concise problem","details":"why it matters and when it occurs","locations":[{"path":"relative/file.py","line":1}]}],"audit":{"completed":true,"scopes":["objective","acceptance_criteria","reviewed_diff","supplied_evidence"]}}
Every finding needs a repository-relative file path and positive 1-based line in the reviewed HEAD. Use an empty findings array when there is no actionable problem. Do not add fields or wrap the JSON in Markdown."""


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
    inference = review_input.get("inference", INFERENCE_ROLE_DEFAULTS["review"])
    progress("review input accepted")

    progress("loading Committed Change and Validation evidence")
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

    reviewed_head = evidence["change"]["repository"]["after"]["head"]

    def validate_response(value: object):
        try:
            if not isinstance(value, str):
                raise TypeError("review response must be JSON text")
            return validate_review(json.loads(value), workspace, reviewed_head)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResponseRejected(str(error)) from error

    inference_result = invoke(
        inference=inference,
        purpose="review",
        trusted_task_instructions=REVIEW_INSTRUCTIONS,
        untrusted_task_data=review_task(review_input, evidence, diff_path),
        requested_capability=Capability.READ_ONLY,
        execution_root=workspace,
        timeout_seconds=review_input["timeout_seconds"],
        evidence_directory=result_directory / "inference",
        validator=validate_response,
    )
    publish_runtime_logs(result_directory, inference_result.receipt)
    progress("review agent completed")

    progress("observing repository after review")
    observation_error = None
    try:
        after = repository_state(workspace)
    except (OSError, subprocess.SubprocessError) as error:
        after = None
        observation_error = str(error)
    unchanged = None if after is None else before == after

    review = inference_result.value if inference_result.outcome == "succeeded" else None
    validation = inference_result.receipt["validation"]
    review_error = (
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
    outcome = (
        "interrupted"
        if inference_result.outcome == "interrupted"
        else "timed_out"
        if inference_result.outcome == "timed_out"
        else "completed"
        if inference_result.outcome == "succeeded"
        and unchanged is True
        and observation_error is None
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
    for field in ("workspace", "change_directory", "validation_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"review {field} must be an absolute path")
    if "inference" in value:
        validate_inference_setting(value["inference"])
    if "related_work" in value:
        validate_reference(value["related_work"])
        validate_snapshot(value["related_work"]["path"], value["related_work"])
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("review timeout_seconds must be a positive integer")


def load_evidence(review_input: dict[str, object]) -> dict[str, object]:
    change = Path(review_input["change_directory"])
    validation = Path(review_input["validation_directory"])
    return {
        "workspace": review_input["workspace"],
        "change_output": read_json(change / "output.json"),
        "validation": read_json(validation / "output.json"),
    }


def verify_subject(before: dict[str, object], evidence: dict[str, object]) -> None:
    try:
        change_output = evidence["change_output"]
        change = validate_change_output(change_output)
        validation = evidence["validation"]
        change_workspace = change["workspace"]
        change_after = subject_state(change["repository"]["after"])
        validation_before = subject_state(validation["repository"]["before"])
        validation_state = subject_state(validation["repository"]["after"])
    except (KeyError, TypeError) as error:
        raise ValueError("invalid Review evidence") from error
    if validation.get("schema_version") != 1:
        raise ValueError("Review Validation must use schema_version 1")
    workspace = Path(change_workspace)
    if workspace.resolve() != Path(evidence["workspace"]).resolve():
        raise ValueError("Review workspace must match Committed Change")
    if validation.get("outcome") != "passed":
        raise ValueError("review Validation must have passed")
    if not (change_after == validation_before == validation_state):
        raise ValueError(
            "Committed Change and Validation must identify one repository state"
        )
    if subject_state(before) != change_after:
        raise ValueError("workspace must match the validated Committed Change state")
    validate_git_transition(
        workspace, change["repository"]["before"], change["repository"]["after"]
    )
    evidence["change"] = change


def subject_state(state: dict[str, object]) -> dict[str, object]:
    if not isinstance(state, dict):
        raise TypeError("repository state must be an object")
    subject = {field: state[field] for field in ("head", "dirty", "status")}
    if (
        not isinstance(subject["head"], str)
        or not subject["head"]
        or not isinstance(subject["dirty"], bool)
        or not isinstance(subject["status"], list)
        or not all(isinstance(line, str) for line in subject["status"])
    ):
        raise ValueError("invalid Review evidence repository state")
    return subject


def write_diff(diff_path: Path, workspace: Path, evidence: dict[str, object]) -> None:
    change = evidence["change"]
    before = change["repository"]["before"]["head"]
    after = change["repository"]["after"]["head"]
    diff = git(
        workspace,
        "diff",
        "--no-ext-diff",
        "--binary",
        f"{before}..{after}",
        "--",
    )
    diff_path.write_text(diff + ("\n" if diff else ""))


def review_task(
    review_input: dict[str, object],
    evidence: dict[str, object],
    diff_path: Path,
) -> dict[str, object]:
    """Build provider-neutral, untrusted task data from verified evidence."""
    change = evidence["change"]
    related = review_input.get("related_work")
    related_records = (
        [json.loads(line) for line in Path(related["path"]).read_text().splitlines()]
        if related is not None
        else []
    )
    return {
        "objective": change["objective"],
        "reviewed_commits": {
            "before": change["repository"]["before"]["head"],
            "after": change["repository"]["after"]["head"],
        },
        "reviewed_diff": diff_path.read_text(),
        "committed_change": evidence["change_output"],
        "validation": evidence["validation"],
        "related_work": related_records,
    }


def related_work_guidance(review_input: dict[str, object]) -> str:
    """Describe the frozen reference without exposing its records as instructions."""
    related = review_input.get("related_work")
    if related is None:
        return ""
    return (
        f"Frozen related-work context: {related['path']} (sha256 {related['sha256']}).\n"
        "The current objective is authoritative. Query that JSONL with jq or rg "
        "only if task ownership or scope is unclear."
    )


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
