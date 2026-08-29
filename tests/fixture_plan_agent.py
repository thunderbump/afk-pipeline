import json
import sys


def proposal():
    return direct_proposal()


def unnecessary_decomposition():
    return {
        "schema_version": 1,
        "decision": "decompose",
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The change is implemented and tested.",
                "statement": "Implement and test the change.",
            }
        ],
        "direct_routes": [],
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


def direct_proposal(criteria=None):
    criteria = criteria or [
        ("The change is implemented and tested.", "Implement and test the change.")
    ]
    return {
        "schema_version": 1,
        "decision": "direct",
        "criteria": [
            {
                "id": f"criterion-{index}",
                "source_text": source,
                "statement": statement,
            }
            for index, (source, statement) in enumerate(criteria, start=1)
        ],
        "direct_routes": [
            {
                "criterion": f"criterion-{index}",
                "project": "afk-pipeline",
                "owner": "AFK implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": evidence,
            }
            for index, evidence in enumerate(
                ["pipeline_run", *["repository_check"] * (len(criteria) - 1)],
                start=1,
            )
        ],
        "children": [],
        "ambiguities": [],
    }


def decomposed_proposal(criteria, children):
    return {
        "schema_version": 1,
        "decision": "decompose",
        "criteria": [
            {
                "id": f"criterion-{index}",
                "source_text": source,
                "statement": statement,
            }
            for index, (source, statement) in enumerate(criteria, start=1)
        ],
        "direct_routes": [],
        "children": children,
        "ambiguities": [],
    }


scenario = sys.argv[1]
if scenario == "invalid-events":
    print("not JSON", flush=True)
    raise SystemExit

if scenario == "direct":
    value = direct_proposal()
elif scenario == "capability-direct":
    value = {
        "schema_version": 2,
        "decision": "direct",
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The change is implemented and tested.",
                "statement": "Implement and test the change.",
            }
        ],
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
elif scenario == "unnecessary-decomposition":
    value = unnecessary_decomposition()
elif scenario == "direct-retry-protocol":
    value = direct_proposal(
        [
            (
                "Retry event cycles are accepted before the final terminal.",
                "Accept retry event cycles before the final terminal.",
            ),
            ("Repository validation passes.", "Pass repository validation."),
        ]
    )
elif scenario == "direct-terminal-decision":
    value = direct_proposal(
        [
            (
                "The terminal decision is recorded.",
                "Record the terminal decision.",
            ),
            (
                "The shared contract is covered by repository validation.",
                "Cover the shared contract with repository validation.",
            ),
        ]
    )
elif scenario == "decompose-project-registration":
    value = decomposed_proposal(
        [
            (
                "The Project and pages are implemented and tested.",
                "Implement and test the Project and pages.",
            ),
            (
                "Deployment and served routes are verified.",
                "Deploy and verify the served routes.",
            ),
        ],
        [
            {
                "local_id": "implementation",
                "title": "Register and render Operations WebUI",
                "objective": "Implement and test the Project registration and pages.",
                "criteria": ["criterion-1"],
                "project": "operations-webui",
                "owner": "Operations implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": [],
            },
            {
                "local_id": "host-closure",
                "title": "Deploy and verify Operations WebUI",
                "objective": "Deploy the accepted change and verify its served routes.",
                "criteria": ["criterion-2"],
                "project": "operations-webui",
                "owner": "Host operator",
                "phase": "closure",
                "execution": "external",
                "evidence_route": "external_check",
                "depends_on": ["implementation"],
                "handoff": {
                    "authority": "Host operator",
                    "subject_fields": ["commit", "environment"],
                    "completion_record": "external_check",
                },
            },
        ],
    )
elif scenario == "decompose-human-approval":
    value = decomposed_proposal(
        [
            (
                "Presentation options are prototyped.",
                "Prototype the presentation options.",
            ),
            ("Brian accepts one direction.", "Accept one presentation direction."),
        ],
        [
            {
                "local_id": "prototype",
                "title": "Prototype paused Run explanations",
                "objective": "Produce the presentation options and source-field analysis.",
                "criteria": ["criterion-1"],
                "project": "operations-webui",
                "owner": "Operations prototype agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": [],
            },
            {
                "local_id": "approval",
                "title": "Choose the paused Run presentation",
                "objective": "Review the prototypes and accept one direction.",
                "criteria": ["criterion-2"],
                "project": "operations-webui",
                "owner": "Brian",
                "phase": "closure",
                "execution": "human",
                "evidence_route": "human_attestation",
                "depends_on": ["prototype"],
                "handoff": {
                    "authority": "Brian",
                    "subject_fields": ["commit"],
                    "completion_record": "human_attestation",
                },
            },
        ],
    )
elif scenario == "decompose-exporter-closure":
    value = decomposed_proposal(
        [
            (
                "The AFK exporter is implemented and tested.",
                "Implement and test the AFK exporter.",
            ),
            (
                "Operations documentation is published.",
                "Publish current Operations documentation.",
            ),
            (
                "The documented site is deployed and verified.",
                "Deploy and verify the documented site.",
            ),
        ],
        [
            {
                "local_id": "afk-implementation",
                "title": "Implement portable v2 export",
                "objective": "Implement and test the accepted exporter contract.",
                "criteria": ["criterion-1"],
                "project": "afk-pipeline",
                "owner": "AFK implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": [],
            },
            {
                "local_id": "operations-docs",
                "title": "Publish exporter documentation",
                "objective": "Document the accepted exporter behavior.",
                "criteria": ["criterion-2"],
                "project": "operations-webui",
                "owner": "Operations documentation agent",
                "phase": "closure",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": ["afk-implementation"],
            },
            {
                "local_id": "host-closure",
                "title": "Deploy and verify exporter documentation",
                "objective": "Deploy and verify the accepted documentation.",
                "criteria": ["criterion-3"],
                "project": "operations-webui",
                "owner": "Host operator",
                "phase": "closure",
                "execution": "external",
                "evidence_route": "external_check",
                "depends_on": ["operations-docs"],
                "handoff": {
                    "authority": "Host operator",
                    "subject_fields": ["commit", "environment"],
                    "completion_record": "external_check",
                },
            },
        ],
    )
else:
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
