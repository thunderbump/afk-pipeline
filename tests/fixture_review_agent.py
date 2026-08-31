import base64
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]

AUDIT = {
    "completed": True,
    "scopes": [
        "objective",
        "acceptance_criteria",
        "reviewed_diff",
        "supplied_evidence",
    ],
}

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
elif scenario in ("null-content", "object-content", "invalid-text-part"):
    content = None
    if scenario == "object-content":
        content = {"type": "text", "text": "not a content array"}
    elif scenario == "invalid-text-part":
        content = [{"type": "text", "text": None}]
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": content,
                },
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
    raise SystemExit
elif scenario in ("mutate-workspace", "damage-git"):
    review = {
        "summary": "No actionable defects found.",
        "findings": [],
        "audit": AUDIT,
    }
    if scenario == "mutate-workspace":
        Path("reviewer-change.txt").write_text("reviewer changed the workspace\n")
    else:
        Path(".git").rename(".git-damaged")
elif scenario in ("no-findings", "delayed-no-findings", "sibling-owned-migration"):
    if scenario == "delayed-no-findings":
        time.sleep(0.2)
    if scenario == "sibling-owned-migration":
        encoded = (
            sys.argv[-1]
            .split('<AFK_UNTRUSTED_TASK_DATA encoding="base64-json">\n', 1)[1]
            .splitlines()[0]
        )
        task = json.loads(base64.b64decode(encoded))
        if not any(
            row.get("relationship") == "sibling"
            and row.get("title") == "Migrate callers"
            for row in task["related_work"]
        ):
            raise SystemExit("caller migration was not sibling-owned")
    review = {
        "summary": (
            "Caller migration belongs to the related sibling; "
            "current change is complete."
            if scenario == "sibling-owned-migration"
            else "No actionable defects found."
        ),
        "findings": [],
        "audit": AUDIT,
    }
elif scenario in (
    "findings",
    "multiple-findings",
    "missing-line",
    "missing-path",
    "outside-path",
    "ignored-path",
    "directory-path",
    "bad-line",
):
    location = {"path": "README.md", "line": 1}
    if scenario == "missing-line":
        location.pop("line")
    elif scenario == "missing-path":
        location["path"] = "missing.py"
    elif scenario == "outside-path":
        location["path"] = "../outside.py"
    elif scenario == "ignored-path":
        location["path"] = "ignored.txt"
    elif scenario == "directory-path":
        location["path"] = "docs"
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
            },
            *(
                [
                    {
                        "severity": "low",
                        "title": "Second fixture finding",
                        "details": "The audit returns every discovered finding together.",
                        "locations": [{"path": "docs/note.txt", "line": 1}],
                    }
                ]
                if scenario == "multiple-findings"
                else []
            ),
        ],
        "audit": AUDIT,
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
