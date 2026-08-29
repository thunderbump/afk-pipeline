import json
import sys

scenario = sys.argv[1]
if scenario == "invalid-events":
    print("not JSON", flush=True)
    raise SystemExit
criteria = [
    {
        "id": "criterion-1",
        "decision": "accepted",
        "rationale": "The implementation completion covers this criterion.",
    },
    {
        "id": "criterion-2",
        "decision": "accepted",
        "rationale": "The scoped external check covers this criterion.",
    },
]
gaps = []
follow_up = None
decision = "accepted"
if scenario == "incomplete":
    criteria[0]["decision"] = "incomplete"
    criteria[0]["rationale"] = "The evidence does not establish the requested behavior."
    gaps = [
        {
            "criterion": "criterion-1",
            "summary": "Implementation evidence does not establish the behavior.",
        }
    ]
    follow_up = {
        "local_id": "follow-up",
        "title": "Complete the missing behavior",
        "objective": "Implement and prove the behavior required by criterion-1.",
        "criteria": ["criterion-1"],
        "project": "example",
        "owner": "Example agent",
        "phase": "implementation",
        "execution": "agent",
        "evidence_route": "pipeline_run",
        "depends_on": [],
    }
    decision = "incomplete"

response = {
    "schema_version": 1,
    "decision": decision,
    "criteria": criteria,
    "gaps": gaps,
    "follow_up": follow_up,
}
print(json.dumps({"type": "agent_start"}), flush=True)
print(
    json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps(response)}],
            },
        }
    ),
    flush=True,
)
print(json.dumps({"type": "agent_end"}), flush=True)
if scenario == "process-failure":
    raise SystemExit(7)
