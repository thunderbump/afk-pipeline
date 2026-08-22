"""Apply the automatic MVP acceptance policy to one canonical plan."""

import json
import sys
import time
from pathlib import Path

from afk_plan_accept.contract import POLICY, PlanNeedsHuman, accept_plan
from afk_runtime import progress, seal_json, timestamp, write_json

USAGE = "usage: python3 -m afk_plan_accept ACCEPTANCE_JSON RESULT_DIRECTORY"
HELP = f"""{USAGE}

Accept one unambiguous, contract-valid Acceptance Plan without mutating Beads.

Arguments:
  ACCEPTANCE_JSON  Exact Planner input and canonical Plan.
  RESULT_DIRECTORY  New directory for accepted input and terminal output.
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
    progress("loading Acceptance Plan policy input")
    request = acceptance_request(json.loads(input_path.read_text()))
    started_at = timestamp()
    started = time.monotonic()
    try:
        acceptance = accept_plan(request["planner_input"], request["plan"])
    except PlanNeedsHuman:
        acceptance = None
        outcome = "unaccepted"
        decision = "needs_human"
        error_category = "plan_ambiguity"
    else:
        outcome = "completed"
        decision = "accepted"
        error_category = None
    progress(f"Acceptance Plan policy decision: {decision}")

    result_directory.mkdir()
    write_json(result_directory / "input.json", request)
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "decision": decision,
        "source": {
            "kind": "bead",
            "id": request["planner_input"]["parent"]["id"],
        },
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "policy": POLICY,
        "acceptance": acceptance,
        "error_category": error_category,
        "artifacts": {"input": "input.json"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} Acceptance Plan policy result at {output_path}")
    return 0 if decision == "accepted" else 1


def acceptance_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "planner_input",
        "plan",
    }:
        raise ValueError("acceptance input has an invalid shape")
    if value["schema_version"] != 1:
        raise ValueError("acceptance input schema_version must be 1")
    return dict(value)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"afk-plan-accept: {error}", file=sys.stderr)
        raise SystemExit(2)
