import json
import sys


def proposal():
    return {
        "schema_version": 1,
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The change is implemented and tested.",
                "statement": "Implement and test the change.",
            }
        ],
        "children": [
            {
                "local_id": "implementation",
                "title": "Implement the change",
                "objective": "Implement and test the requested behavior.",
                "criteria": ["criterion-1"],
                "project": "afk-pipeline",
                "owner": "AFK implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": [],
            }
        ],
        "ambiguities": [],
    }


scenario = sys.argv[1]
if scenario == "invalid-events":
    print("not JSON", flush=True)
    raise SystemExit

value = proposal()
if scenario == "invalid-proposal":
    value["criteria"][0]["source_text"] = "Only part of the requirement."

print(json.dumps({"type": "agent_start"}), flush=True)
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps(value)}],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
if scenario == "process-failure":
    raise SystemExit(7)
