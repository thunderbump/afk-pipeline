import base64
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
elif scenario in ("address", "capture-prompt", "delayed-address"):
    if scenario == "delayed-address":
        time.sleep(0.2)
    elif scenario == "capture-prompt":
        encoded = (
            sys.argv[-1]
            .split('<AFK_UNTRUSTED_TASK_DATA encoding="base64-json">\n', 1)[1]
            .splitlines()[0]
        )
        Path(sys.argv[2]).write_text(json.dumps(json.loads(base64.b64decode(encoded))))
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
elif scenario in ("dismiss", "sibling-owned-finding"):
    rationale = "The claimed behavior is not reachable."
    if scenario == "sibling-owned-finding":
        prompt = sys.argv[-1]
        encoded = prompt.split('<AFK_UNTRUSTED_TASK_DATA encoding="base64-json">\n', 1)[
            1
        ].splitlines()[0]
        task = json.loads(base64.b64decode(encoded))
        if not any(
            row.get("relationship") == "sibling"
            and row.get("title") == "Migrate callers"
            for row in task["related_work"]
        ):
            raise SystemExit("caller migration was not sibling-owned")
        if "current implementation objective is authoritative" not in prompt or (
            "work owned by a sibling task" not in prompt
        ):
            raise SystemExit("trusted sibling-ownership policy was omitted")
        rationale = "The requested caller migration is owned by a sibling task."
    assessment = {
        "summary": "The finding should not be addressed.",
        "decisions": [
            {
                "finding_index": 0,
                "worth_addressing": False,
                "rationale": rationale,
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
