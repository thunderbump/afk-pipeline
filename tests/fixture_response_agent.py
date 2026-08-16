import json
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]


def valid_response():
    return {
        "summary": "Addressed the actionable finding.",
        "finding_responses": [
            {
                "finding_index": 0,
                "response": "Updated the implementation and committed the change.",
            }
        ],
    }


def commit_response():
    Path("README.md").write_text("reviewed code\nresponse applied\n")
    subprocess.run(["git", "add", "README.md"], check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Respond to feedback"], check=True
    )


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
elif scenario == "damage-git":
    Path(".git").rename(".git-damaged")
    response = valid_response()
elif scenario in {"commit", "delayed-commit"}:
    if scenario == "delayed-commit":
        time.sleep(0.2)
    commit_response()
    response = valid_response()
elif scenario == "capture-prompt":
    Path(sys.argv[2]).write_text(sys.argv[-1])
    commit_response()
    response = valid_response()
elif scenario == "dirty":
    Path("README.md").write_text("uncommitted response\n")
    response = valid_response()
elif scenario == "unchanged":
    response = valid_response()
elif scenario == "invalid-json":
    response = "not JSON"
elif scenario == "missing-response":
    response = {"summary": "Incomplete.", "finding_responses": []}
elif scenario == "wrong-index":
    response = {
        "summary": "Wrong finding.",
        "finding_responses": [{"finding_index": 1, "response": "Changed it."}],
    }
else:
    raise SystemExit(f"unknown response fixture scenario: {scenario}")

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
                            response
                            if isinstance(response, str)
                            else json.dumps(response)
                        ),
                    }
                ],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
