import json
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]

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
