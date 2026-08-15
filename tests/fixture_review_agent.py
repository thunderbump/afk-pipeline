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
    review = {"summary": "No actionable defects found.", "findings": []}
    if scenario == "mutate-workspace":
        Path("reviewer-change.txt").write_text("reviewer changed the workspace\n")
    else:
        Path(".git").rename(".git-damaged")
elif scenario in ("no-findings", "delayed-no-findings"):
    if scenario == "delayed-no-findings":
        time.sleep(0.2)
    review = {"summary": "No actionable defects found.", "findings": []}
elif scenario in (
    "findings",
    "missing-line",
    "missing-path",
    "outside-path",
    "bad-line",
):
    location = {"path": "README.md", "line": 1}
    if scenario == "missing-line":
        location.pop("line")
    elif scenario == "missing-path":
        location["path"] = "missing.py"
    elif scenario == "outside-path":
        location["path"] = "../outside.py"
    elif scenario == "bad-line":
        location["line"] = 999
    review = {
        "summary": "One actionable defect found.",
        "findings": [
            {
                "severity": "medium",
                "title": "Fixture finding",
                "details": "The fixture demonstrates a line-anchored finding.",
                "locations": [location],
            }
        ],
    }
else:
    raise SystemExit(f"unknown review fixture scenario: {scenario}")

print(json.dumps({"type": "agent_start"}), flush=True)
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps(review)}],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
