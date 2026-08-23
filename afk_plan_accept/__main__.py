"""Apply deterministic policy to direct routing or one canonical Plan."""

import json
import sys
import time
from pathlib import Path

from afk_plan_accept.contract import (
    DIRECT_POLICY,
    POLICY,
    PlanNeedsHuman,
    RoutingNeedsHuman,
    accept_direct,
    accept_plan,
)
from afk_runtime import progress, seal_json, timestamp, write_json

USAGE = "usage: python3 -m afk_plan_accept ACCEPTANCE_JSON RESULT_DIRECTORY"
HELP = f"""{USAGE}

Accept direct routing or one unambiguous canonical Plan without mutating Beads.

Arguments:
  ACCEPTANCE_JSON  Exact Planner input and direct Routing or canonical Plan.
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
    policy = DIRECT_POLICY if "routing" in request else POLICY
    try:
        if "routing" in request:
            acceptance = accept_direct(request["planner_input"], request["routing"])
            accepted_decision = "direct"
        else:
            acceptance = accept_plan(request["planner_input"], request["plan"])
            accepted_decision = "accepted"
    except PlanNeedsHuman:
        acceptance = None
        outcome = "unaccepted"
        decision = "needs_human"
        error_category = "plan_ambiguity"
    except RoutingNeedsHuman:
        acceptance = None
        outcome = "unaccepted"
        decision = "needs_human"
        error_category = "direct_incompatible"
    else:
        outcome = "completed"
        decision = accepted_decision
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
        "policy": policy,
        "acceptance": acceptance,
        "error_category": error_category,
        "artifacts": {"input": "input.json"},
    }
    output_path = result_directory / "output.json"
    seal_json(output_path, output)
    progress(f"sealed {outcome} Acceptance Plan policy result at {output_path}")
    return 0 if decision in {"accepted", "direct"} else 1


def acceptance_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) not in (
        {"schema_version", "planner_input", "plan"},
        {"schema_version", "planner_input", "routing"},
    ):
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
