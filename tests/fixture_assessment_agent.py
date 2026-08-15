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
elif scenario in ("mutate-workspace", "damage-git"):
    assessment = {
        "summary": "The finding should be addressed.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": True,
                "rationale": "The behavior is concrete and reachable.",
            }
        ],
    }
    if scenario == "mutate-workspace":
        Path("assessor-change.txt").write_text("assessor changed the workspace\n")
    else:
        Path(".git").rename(".git-damaged")
elif scenario in ("address", "delayed-address"):
    if scenario == "delayed-address":
        time.sleep(0.2)
    assessment = {
        "summary": "The finding should be addressed.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": True,
                "rationale": "The behavior is reachable and violates the objective.",
            }
        ],
    }
elif scenario == "no-findings":
    assessment = {"summary": "Nothing to assess.", "decisions": []}
elif scenario == "dismiss":
    assessment = {
        "summary": "The finding should not be addressed.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": False,
                "rationale": "The claimed behavior is not reachable.",
            }
        ],
    }
elif scenario == "mixed":
    assessment = {
        "summary": "One finding is actionable and one is not.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": True,
                "rationale": "The first finding is concrete and reachable.",
            },
            {
                "finding_index": 1,
                "worth_addressing": False,
                "rationale": "The second finding is preference-only feedback.",
            },
        ],
    }
elif scenario in ("missing-decision", "duplicate-decision", "invalid-decision"):
    first = {
        "finding_index": 0,
        "worth_addressing": True,
        "rationale": "The first finding is actionable.",
    }
    decisions = [first]
    if scenario == "duplicate-decision":
        decisions.append(first)
    elif scenario == "invalid-decision":
        first["worth_addressing"] = "yes"
        decisions.append(
            {
                "finding_index": 1,
                "worth_addressing": False,
                "rationale": "The second finding is not actionable.",
            }
        )
    assessment = {"summary": "Invalid fixture assessment.", "decisions": decisions}
elif scenario == "invalid-json":
    assessment = "not JSON"
else:
    raise SystemExit(f"unknown assessment fixture scenario: {scenario}")

print(json.dumps({"type": "agent_start"}), flush=True)
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            assessment
                            if isinstance(assessment, str)
                            else json.dumps(assessment)
                        ),
                    }
                ],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
