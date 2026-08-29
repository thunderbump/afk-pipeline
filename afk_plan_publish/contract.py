"""Load and revalidate immutable Acceptance Plan evidence."""

import json
from pathlib import Path

from afk_plan.contract import bounded_text, object_with_keys, utc_timestamp
from afk_plan_accept.contract import accept_plan, validate_accepted_output


def load_accepted_plan(directory: Path) -> tuple[dict[str, object], dict[str, object]]:
    request = json.loads((directory / "input.json").read_text())
    output = json.loads((directory / "output.json").read_text())
    if (
        not isinstance(request, dict)
        or set(request) != {"schema_version", "planner_input", "plan"}
        or request["schema_version"] not in {1, 2}
        or not isinstance(request["planner_input"], dict)
        or request["schema_version"] != request["planner_input"].get("schema_version")
    ):
        raise ValueError("Acceptance Plan input has an invalid shape")
    accepted = accept_plan(request["planner_input"], request["plan"])
    output = validate_accepted_output(request["planner_input"], output)
    if output["acceptance"] != accepted:
        raise ValueError("accepted-plan record does not match its evidence")
    return request["planner_input"], accepted


def validate_published_output(
    value: object, parent_id: str, acceptance: dict[str, object]
) -> dict[str, object]:
    publication = object_with_keys(
        value,
        {
            "schema_version",
            "outcome",
            "decision",
            "source",
            "started_at",
            "finished_at",
            "duration_seconds",
            "acceptance_sha256",
            "plan_sha256",
            "children",
            "error_category",
            "artifacts",
        },
        "Child Graph Publisher output",
    )
    plan = acceptance["plan"]
    if (
        publication["schema_version"] != 1
        or publication["outcome"] != "completed"
        or publication["decision"] not in {"published", "replayed"}
        or publication["source"] != {"kind": "bead", "id": parent_id}
        or publication["acceptance_sha256"] != acceptance["acceptance_sha256"]
        or publication["plan_sha256"] != plan["plan_sha256"]
        or publication["error_category"] is not None
        or publication["artifacts"]
        != {
            "input": "input.json",
            "stdout": "stdout.log.json",
            "stderr": "stderr.log.json",
        }
    ):
        raise ValueError("Child Graph Publisher output is not successful")
    for field in ("started_at", "finished_at"):
        utc_timestamp(publication[field], f"publication {field}")
    duration = publication["duration_seconds"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("publication duration_seconds is invalid")
    mappings = publication["children"]
    if not isinstance(mappings, list) or len(mappings) != len(plan["children"]):
        raise ValueError("published child mapping does not cover the plan")
    accepted = []
    local_ids = set()
    bead_ids = set()
    for mapping_value in mappings:
        mapping = object_with_keys(
            mapping_value, {"local_id", "bead_id"}, "child mapping"
        )
        bounded_text(mapping["local_id"], "child mapping local_id", 128)
        bounded_text(mapping["bead_id"], "child mapping bead_id", 256)
        if mapping["local_id"] in local_ids or mapping["bead_id"] in bead_ids:
            raise ValueError("published child mapping identities must be unique")
        local_ids.add(mapping["local_id"])
        bead_ids.add(mapping["bead_id"])
        accepted.append(mapping)
    if local_ids != {child["local_id"] for child in plan["children"]}:
        raise ValueError("published child mapping does not match the plan")
    publication["children"] = accepted
    return publication


def external_reference(plan_sha256: str, local_id: str) -> str:
    return f"afk-plan:{plan_sha256}:{local_id}"


def child_acceptance(plan: dict[str, object], child: dict[str, object]) -> str:
    criteria = {item["id"]: item for item in plan["criteria"]}
    statements = [f"- {criteria[item]['statement']}" for item in child["criteria"]]
    if child.get("execution") == "external":
        statements.append(
            "- Valid external-check evidence must be attached before this child closes."
        )
    if child.get("executor") == "outside_help":
        statements.append(
            "- external_check evidence of the work performed by the named outside source must be attached before this child closes."
        )
    return "\n".join(statements)


def child_description(
    parent_id: str,
    plan: dict[str, object],
    child: dict[str, object],
    bead_id: str | None = None,
) -> str:
    criteria = {item["id"]: item for item in plan["criteria"]}
    lines = [
        child["objective"],
        "",
        "## Parent acceptance criteria",
        *[f"- {criteria[item]['source_text']}" for item in child["criteria"]],
    ]
    if plan["schema_version"] == 2 and child["executor"] == "outside_help":
        lines.extend(
            [
                "",
                "## Outside capability required",
                "",
                "The agent system cannot perform this child with its available capabilities.",
                f"- Unavailable capability reason: `{child['outside_help_reason']}`",
                f"- Expected outside source: `{child['owner']}`",
                f"- Performed-work evidence route: `{child['evidence_route']}`",
                f"- Parent Bead: `{parent_id}`",
                f"- Parent plan: `{plan['plan_sha256']}`",
                *([f"- Child Bead: `{bead_id}`"] if bead_id is not None else []),
                "",
                "Attach external_check evidence of the work performed before closing this child.",
            ]
        )
    if (
        plan["schema_version"] == 1
        and child["execution"] != "agent"
        and bead_id is not None
    ):
        handoff = child["handoff"]
        heading = "External completion handoff"
        ownership = "This child is completed by the named external authority."
        subject = {field: f"<{field}>" for field in handoff["subject_fields"]}
        record = {
            "schema_version": 1,
            "child": bead_id,
            "parent_plan": plan["plan_sha256"],
            "outcome": "satisfied",
            "producer": {
                "kind": handoff["completion_record"],
                "identity": handoff["authority"],
            },
            "criteria": child["criteria"],
            "subject": subject,
            "evidence": [f"<{handoff['completion_record']}-evidence>"],
            "accepted_at": "<timestamp>",
        }
        lines.extend(
            [
                "",
                f"## {heading}",
                "",
                ownership,
                f"- Parent Bead: `{parent_id}`",
                f"- Parent plan: `{plan['plan_sha256']}`",
                f"- Child Bead: `{bead_id}`",
                f"- Expected authority: `{handoff['authority']}`",
                f"- Required subject fields: `{', '.join(handoff['subject_fields'])}`",
                f"- Completion record kind: `{handoff['completion_record']}`",
                "",
                "Attach this structured Completion Record before closing the child:",
                "",
                "```json",
                json.dumps(record, indent=2),
                "```",
                "",
                "A changed parent plan or referenced subject requires new evidence.",
            ]
        )
    return "\n".join(lines)
