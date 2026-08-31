"""Review verified child fan-in against one accepted parent intent."""

import json
import shutil
import sys
import time
from pathlib import Path

from afk_inference import invoke
from afk_parent_review.contract import (
    load_fan_in,
    load_request,
    overlaps,
    request_for_output,
)
from afk_parent_review.task import build_task
from afk_runtime import (
    process_result,
    progress,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_parent_review REVIEW_JSON RESULT_DIRECTORY"
HELP = f"""{USAGE}

Judge whether verified child outcomes collectively satisfy one accepted parent Plan.

Arguments:
  REVIEW_JSON  Accepted Plan, published graph, closed child graph, and Completion results.
  RESULT_DIRECTORY  New immutable attempt directory for input, agent evidence, and output.
"""


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    return _runtime_main()


def _runtime_main() -> int:
    input_path = Path(sys.argv[1])
    result = Path(sys.argv[2])
    progress("loading Parent Acceptance Review evidence")
    request = load_request(json.loads(input_path.read_text()))
    validate_result_location(result, request["protected_directories"])
    fan_in = load_fan_in(request)
    task = build_task(fan_in)
    progress("Parent Acceptance Review evidence accepted")

    result.mkdir()
    write_json(result / "input.json", request_for_output(request))
    write_json(result / "fan-in.json", fan_in)
    started_at = timestamp()
    started = time.monotonic()
    progress(
        f"starting Parent Acceptance Review (timeout={request['timeout_seconds']}s)"
    )

    inference_result = invoke(
        purpose=task.purpose,
        task_contract_version=task.contract_version,
        trusted_task_instructions=task.trusted_instructions,
        untrusted_task_data=task.untrusted_data,
        requested_capability=task.capability,
        execution_root=input_path.parent,
        timeout_seconds=request["timeout_seconds"],
        evidence_directory=result / "inference",
        validator=task.validator,
    )
    progress("Parent Acceptance Review inference stopped")
    _publish_runtime_logs(result, inference_result.receipt)

    review = inference_result.value if inference_result.outcome == "succeeded" else None
    if inference_result.outcome == "succeeded":
        outcome, decision, error_category = "completed", review["decision"], None
    elif inference_result.outcome == "interrupted":
        outcome, decision, error_category = "interrupted", "incomplete", "agent_process"
    elif inference_result.outcome == "timed_out":
        outcome, decision, error_category = "timed_out", "incomplete", "agent_process"
    elif inference_result.outcome == "response_rejected":
        outcome, decision, error_category = "failed", "incomplete", "invalid_review"
    else:
        status = inference_result.receipt["protocol"].get("status")
        category = (
            "agent_protocol"
            if status in {"protocol_malformed", "response_missing"}
            else "agent_process"
        )
        outcome, decision, error_category = "failed", "incomplete", category

    terminal = inference_result.receipt["terminal_response"]
    agent = (
        {"status": "completed"}
        if terminal is not None
        and inference_result.receipt["protocol"].get("status") == "accepted"
        else None
    )
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "decision": decision,
        "source": {"kind": "bead", "id": fan_in["parent"]["id"]},
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "plan_sha256": fan_in["plan_sha256"],
        "review": review,
        "process": _runtime_process(inference_result.receipt),
        "agent": agent,
        "reviewer": {
            "kind": "inference",
            "provider": "openai-codex",
            "model": inference_result.receipt["identity"]["model"],
            "status": outcome,
        },
        "error_category": error_category,
        "artifacts": {
            "input": "input.json",
            "fan_in": "fan-in.json",
            "events": "events.jsonl",
            "stderr": "stderr.log",
        },
    }
    seal_json(result / "output.json", output)
    progress(f"sealed {decision} Parent Acceptance Review at {result / 'output.json'}")
    return 0 if decision == "accepted" else 1


def _publish_runtime_logs(result: Path, receipt: object) -> None:
    attempts = receipt["attempts"]
    for name in ("events", "stderr"):
        target = result / ("events.jsonl" if name == "events" else "stderr.log")
        source = attempts[-1]["artifacts"].get(name) if attempts else None
        if source:
            shutil.copyfile(result / "inference" / source, target)
        else:
            target.touch()


def _runtime_process(receipt: object) -> dict[str, object]:
    attempts = receipt["attempts"]
    process = attempts[-1].get("process", {}) if attempts else {}
    return process_result(process.get("exit_code"), process.get("error"))


def validate_result_location(result: Path, protected: list[Path]) -> None:
    if result.exists() or not result.is_absolute() or not result.parent.is_dir():
        raise ValueError("result directory must be an absolute new path")
    resolved = result.parent.resolve() / result.name
    if any(overlaps(resolved, directory) for directory in protected):
        raise ValueError("result directory must be outside source evidence")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"afk-parent-review: {error}", file=sys.stderr)
        raise SystemExit(2)
