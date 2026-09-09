"""Synthetic Pi subprocess for current Attempt execution and metrics tests."""

import json
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]
if scenario == "hang":
    Path("worker-started").write_text("started")
    time.sleep(60)
elif scenario == "fail":
    raise SystemExit(7)
elif scenario == "malformed":
    print("not-json")
    raise SystemExit
elif scenario == "commit":
    readme = Path("README.md")
    readme.write_text(readme.read_text() + "measured attempt\n")
    subprocess.run(["git", "add", "README.md"], check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "Synthetic Attempt"], check=True)
elif scenario != "empty":
    raise SystemExit("unknown scenario")
print(json.dumps({"type": "agent_start"}))
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "id": "attempt-message",
                "provider": "synthetic-provider",
                "model": "synthetic-model",
                "content": [
                    {
                        "type": "text",
                        "text": ""
                        if scenario == "empty"
                        else "Implemented and committed the fixture change.",
                    }
                ],
                "usage": {
                    "input": 7,
                    "output": 2,
                    "cacheRead": 3,
                    "cacheWrite": 0,
                    "totalTokens": 12,
                    "reasoning": 0,
                    "cost": {"total": 0.001},
                },
            },
        }
    )
)
print(json.dumps({"type": "agent_end"}))
