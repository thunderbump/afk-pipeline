import json
import signal
import subprocess
import sys
import time
from pathlib import Path

scenario = sys.argv[1]

if scenario == "success":
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
elif scenario == "agent-error":
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "fixture agent error",
                },
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
elif scenario == "agent-aborted":
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "aborted"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
elif scenario == "process-failure":
    print("fixture process failed", file=sys.stderr, flush=True)
    raise SystemExit(7)
elif scenario == "invalid-events":
    print("not JSON", flush=True)
elif scenario == "invalid-event-shape":
    print(json.dumps({"type": "message_end", "message": None}), flush=True)
elif scenario == "invalid-event-encoding":
    sys.stdout.buffer.write(b"\xff\n")
    sys.stdout.buffer.flush()
elif scenario == "events-after-end":
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
    print(json.dumps({"type": "queue_update"}), flush=True)
elif scenario == "hang":
    marker = Path(sys.argv[2])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=False,
    )
    marker.write_text(str(child.pid))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    while True:
        time.sleep(1)
elif scenario == "git-commit":
    Path("result.txt").write_text("fixture result\n")
    subprocess.run(["git", "add", "result.txt"], check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Fixture implementation"], check=True
    )
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
elif scenario == "damage-git":
    Path(".git").rename(".git-damaged")
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
elif scenario == "damage-history":
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    Path("result.txt").write_text("new history\n")
    subprocess.run(
        ["git", "checkout", "--orphan", "replacement"], check=True, capture_output=True
    )
    subprocess.run(["git", "add", "result.txt"], check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Replacement history"], check=True
    )
    (Path(".git") / "objects" / before[:2] / before[2:]).unlink()
    print(json.dumps({"type": "agent_start"}), flush=True)
    print(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
        flush=True,
    )
    print(json.dumps({"type": "agent_end"}), flush=True)
else:
    raise SystemExit(f"unknown fixture scenario: {scenario}")
