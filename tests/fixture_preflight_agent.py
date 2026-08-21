import json
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]

if scenario in {
    "counted-proceed",
    "counted-pause",
    "counted-slow-proceed",
    "counted-lock-holder",
    "counted-too-many",
    "counted-valid-failure",
}:
    marker = Path(sys.argv[2])
    with marker.open("a") as stream:
        stream.write("invoked\n")
    if scenario == "counted-slow-proceed":
        time.sleep(0.25)
    elif scenario == "counted-lock-holder":
        time.sleep(2)
    if scenario == "counted-too-many":
        scenario = "too-many"
    elif scenario == "counted-valid-failure":
        scenario = "valid-failure"
    else:
        scenario = "pause" if scenario == "counted-pause" else "proceed"

if scenario == "hang":
    marker = Path(sys.argv[2])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker.write_text(str(child.pid))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    while True:
        time.sleep(1)
elif scenario == "invalid-events":
    print("not JSON", flush=True)
    raise SystemExit
elif scenario == "proceed":
    classification = {
        "schema_version": 1,
        "requests": [
            {
                "index": 1,
                "request": "The terminal decision is recorded and tested.",
                "category": "repository_validation",
                "route": "repository validation",
                "rationale": "The configured repository command proves the behavior.",
            },
            {
                "index": 2,
                "request": "Validation is shared through one contract module.",
                "category": "pipeline_evidence",
                "route": "AFK committed change and Review",
                "rationale": "The committed change and Review prove module ownership.",
            },
        ],
    }
elif scenario == "pause":
    classification = {
        "schema_version": 1,
        "requests": [
            {
                "index": 1,
                "request": "Tests pass.",
                "category": "repository_validation",
                "route": "repository validation",
                "rationale": "The repository command proves its tests.",
            },
            *[
                {
                    "index": index,
                    "request": f"{name} passes.",
                    "category": "operator_external",
                    "route": "operator handoff",
                    "rationale": f"{name} is owned by the operator handoff.",
                }
                for index, name in enumerate(
                    ("Build", "Deployment", "HTTP verification"), start=2
                )
            ],
        ],
    }
elif scenario == "too-many":
    classification = {
        "schema_version": 1,
        "requests": [
            {
                "index": index,
                "request": f"Request {index} is proven.",
                "category": "repository_validation",
                "route": "repository validation",
                "rationale": "The repository validation route proves it.",
            }
            for index in range(1, 258)
        ],
    }
elif scenario == "valid-failure":
    classification = {
        "schema_version": 1,
        "requests": [
            {
                "index": 1,
                "request": "The terminal decision is recorded and tested.",
                "category": "repository_validation",
                "route": "repository validation",
                "rationale": "The configured repository command proves the behavior.",
            }
        ],
    }
elif scenario == "invalid-classification":
    classification = {"schema_version": 1, "requests": []}
else:
    raise SystemExit(f"unknown preflight fixture scenario: {scenario}")

print(json.dumps({"type": "agent_start"}), flush=True)
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps(classification)}],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
if scenario == "valid-failure":
    raise SystemExit(7)
