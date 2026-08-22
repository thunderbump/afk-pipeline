"""Load and revalidate immutable Acceptance Plan evidence."""

import json
from pathlib import Path

from afk_plan_accept.contract import accept_plan, validate_accepted_output


def load_accepted_plan(directory: Path) -> tuple[dict[str, object], dict[str, object]]:
    request = json.loads((directory / "input.json").read_text())
    output = json.loads((directory / "output.json").read_text())
    if (
        not isinstance(request, dict)
        or set(request) != {"schema_version", "planner_input", "plan"}
        or request["schema_version"] != 1
    ):
        raise ValueError("Acceptance Plan input has an invalid shape")
    accepted = accept_plan(request["planner_input"], request["plan"])
    output = validate_accepted_output(request["planner_input"]["parent"]["id"], output)
    if output["acceptance"] != accepted:
        raise ValueError("accepted-plan record does not match its evidence")
    return request["planner_input"], accepted


def external_reference(plan_sha256: str, local_id: str) -> str:
    return f"afk-plan:{plan_sha256}:{local_id}"


def child_acceptance(plan: dict[str, object], child: dict[str, object]) -> str:
    criteria = {item["id"]: item for item in plan["criteria"]}
    statements = [f"- {criteria[item]['statement']}" for item in child["criteria"]]
    if child["execution"] == "human":
        statements.append(
            "- A valid Completion Record must be attached before this child closes."
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
    if child["execution"] != "agent" and bead_id is not None:
        handoff = child["handoff"]
        heading = (
            "Human completion handoff"
            if child["execution"] == "human"
            else "External completion handoff"
        )
        ownership = (
            "This child is not agent-executable."
            if child["execution"] == "human"
            else "This child is completed by the named external authority."
        )
        subject = {field: f"<{field}>" for field in handoff["subject_fields"]}
        record = {
            "schema_version": 1,
            "child": bead_id,
            "parent_plan": plan["plan_sha256"],
            "outcome": "accepted",
            "authority": {
                "kind": child["execution"],
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
                "This scoped approval is consumed once. A changed parent plan or",
                "referenced subject requires new approval.",
            ]
        )
    return "\n".join(lines)
