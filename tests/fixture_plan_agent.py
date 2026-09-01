import json
import sys

scenario = sys.argv[1]
if scenario == "invalid-events":
    print("not JSON", flush=True)
    raise SystemExit

criterion = {
    "id": "criterion-1",
    "source_text": "The change is implemented and tested.",
    "statement": "Implement and test the change.",
}
value = {
    "schema_version": 2,
    "decision": "direct",
    "criteria": [criterion],
    "direct_routes": [
        {
            "criterion": "criterion-1",
            "project": "afk-pipeline",
            "owner": "AFK Run",
            "phase": "implementation",
            "executor": "afk_run",
            "evidence_route": "pipeline_run",
        }
    ],
    "children": [],
    "ambiguities": [],
}

if scenario == "capability-fan-in":
    value = {
        "schema_version": 2,
        "decision": "decompose",
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The caller repository check passes.",
                "statement": "Pass the caller repository check.",
            },
            {
                "id": "criterion-2",
                "source_text": "The outside-helper repository check passes.",
                "statement": "Pass the outside-helper repository check.",
            },
        ],
        "direct_routes": [],
        "children": [
            {
                "local_id": "implementation",
                "title": "Check the implementation repository",
                "objective": "Run the implementation repository check.",
                "criteria": ["criterion-1"],
                "project": "example",
                "owner": "Caller agent",
                "phase": "implementation",
                "executor": "caller_agent",
                "evidence_route": "repository_check",
                "depends_on": [],
            },
            {
                "local_id": "outside-check",
                "title": "Check the repository with outside help",
                "objective": "Obtain the outside repository check.",
                "criteria": ["criterion-2"],
                "project": "example",
                "owner": "Credential holder",
                "phase": "closure",
                "executor": "outside_help",
                "outside_help_reason": "missing_credentials",
                "evidence_route": "external_check",
                "depends_on": ["implementation"],
            },
        ],
        "ambiguities": [],
    }
elif scenario in {
    "capability-run-direct",
    "capability-run-decompose",
    "capability-run-clarification",
    "capability-run-outside-help",
}:
    criterion = {
        "id": "criterion-1",
        "source_text": "Commit the result.",
        "statement": "Commit the result.",
    }
    if scenario == "capability-run-decompose":
        value = {
            "schema_version": 2,
            "decision": "decompose",
            "criteria": [criterion],
            "direct_routes": [],
            "children": [
                {
                    "local_id": "caller-work",
                    "title": "Complete automated host closure",
                    "objective": "Run the automated host check after repository work.",
                    "criteria": ["criterion-1"],
                    "project": "fixture",
                    "owner": "Caller agent",
                    "phase": "closure",
                    "executor": "caller_agent",
                    "evidence_route": "external_check",
                    "depends_on": [],
                }
            ],
            "ambiguities": [],
        }
    else:
        outside = scenario == "capability-run-outside-help"
        value = {
            "schema_version": 2,
            "decision": "direct",
            "criteria": [criterion],
            "direct_routes": [
                {
                    "criterion": "criterion-1",
                    "project": "fixture",
                    "owner": "Credential holder" if outside else "AFK Run",
                    "phase": "closure" if outside else "implementation",
                    "executor": "outside_help" if outside else "afk_run",
                    **(
                        {"outside_help_reason": "missing_credentials"}
                        if outside
                        else {}
                    ),
                    "evidence_route": "external_check" if outside else "pipeline_run",
                }
            ],
            "children": [],
            "ambiguities": (
                ["The requested target is unclear."]
                if scenario == "capability-run-clarification"
                else []
            ),
        }

if scenario in {"invalid", "invalid-proposal"}:
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
