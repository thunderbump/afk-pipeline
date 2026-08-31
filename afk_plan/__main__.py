"""Run one inference-assisted Acceptance Planner and seal its proposed plan."""

import json
import shutil
import sys
import time
from pathlib import Path

from afk_inference import invoke
from afk_plan.contract import validate_input
from afk_plan.task import build_task
from afk_runtime import (
    process_result,
    progress,
    seal_json,
    timestamp,
    write_json,
)

USAGE = "usage: python3 -m afk_plan PLANNER_JSON RESULT_DIRECTORY"
HELP = f"""{USAGE}

Route one frozen Bead directly or propose child work without mutating Beads.

Arguments:
  PLANNER_JSON  Structured parent Bead, trusted project/route catalog, and timeout.
  RESULT_DIRECTORY  New directory for accepted input, raw agent evidence, and output.
"""


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    return _runtime_main()


def _runtime_main() -> int:
    input_path = Path(sys.argv[1])
    result_directory = Path(sys.argv[2])
    progress("loading Acceptance Planner input")
    request = validate_input(json.loads(input_path.read_text()))
    task = build_task(request)
    progress("Acceptance Planner input accepted")

    result_directory.mkdir()
    write_json(result_directory / "input.json", request)
    started_at = timestamp()
    started = time.monotonic()
    progress(f"starting Acceptance Planner (timeout={request['timeout_seconds']}s)")

    inference_result = invoke(
        purpose=task.purpose,
        task_contract_version=task.contract_version,
        trusted_task_instructions=task.trusted_instructions,
        untrusted_task_data=task.untrusted_data,
        requested_capability=task.capability,
        execution_root=input_path.parent,
        timeout_seconds=request["timeout_seconds"],
        evidence_directory=result_directory / "inference",
        validator=task.validator,
    )
    progress("Acceptance Planner inference stopped")
    _publish_runtime_logs(result_directory, inference_result.receipt)

    routing = plan = None
    if inference_result.outcome == "succeeded":
        routing, plan = inference_result.value
        outcome, error_category = "completed", None
    elif inference_result.outcome == "interrupted":
        outcome, error_category = "interrupted", "agent_process"
    elif inference_result.outcome == "timed_out":
        outcome, error_category = "timed_out", "agent_process"
    elif inference_result.outcome == "response_rejected":
        outcome, error_category = "failed", "invalid_proposal"
    else:
        outcome = "failed"
        error_category = _runtime_error_category(inference_result.receipt)

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
        "source": {"kind": "bead", "id": request["parent"]["id"]},
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "process": _runtime_process(inference_result.receipt),
        "agent": agent,
        "planner": {
            "kind": "inference",
            "provider": "openai-codex",
            "model": inference_result.receipt["identity"]["model"],
            "status": outcome,
        },
        "routing": routing,
        "plan": plan,
        "error_category": error_category,
        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} Acceptance Planner result at {output_path}")
    return 0 if outcome == "completed" else 1


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


def _runtime_error_category(receipt: object) -> str:
    status = receipt["protocol"].get("status")
    return (
        "agent_protocol"
        if status in {"protocol_malformed", "response_missing"}
        else "agent_process"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"afk-plan: {error}", file=sys.stderr)
        raise SystemExit(2)
