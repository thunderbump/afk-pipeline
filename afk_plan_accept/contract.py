"""Accept structurally valid, unambiguous Acceptance Plans without I/O."""

from afk_plan.contract import digest, validate_input, validate_plan

POLICY = "contract-valid-proposed-v1"
OUTPUT_FIELDS = {
    "schema_version",
    "outcome",
    "decision",
    "source",
    "started_at",
    "finished_at",
    "duration_seconds",
    "policy",
    "acceptance",
    "error_category",
    "artifacts",
}


class PlanNeedsHuman(ValueError):
    """Raised when a valid plan retains inference ambiguity."""


def accept_plan(planner_input: object, plan: object) -> dict[str, object]:
    request = validate_input(planner_input)
    validated_plan = validate_plan(request, plan)
    if validated_plan["status"] != "proposed" or validated_plan["ambiguities"]:
        raise PlanNeedsHuman("plan needs human interpretation")
    body = {
        "schema_version": 1,
        "status": "accepted",
        "policy": POLICY,
        "basis": "structural_validity_only",
        "parent": validated_plan["parent"],
        "catalog_sha256": validated_plan["catalog_sha256"],
        "plan_sha256": validated_plan["plan_sha256"],
        "plan": validated_plan,
    }
    return {**body, "acceptance_sha256": digest(body)}


def validate_accepted_output(parent_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != OUTPUT_FIELDS:
        raise ValueError("Acceptance Plan policy output has an invalid shape")
    output = dict(value)
    if (
        output["schema_version"] != 1
        or output["outcome"] != "completed"
        or output["decision"] != "accepted"
        or output["source"] != {"kind": "bead", "id": parent_id}
        or output["policy"] != POLICY
        or output["error_category"] is not None
        or output["artifacts"] != {"input": "input.json"}
    ):
        raise ValueError("Acceptance Plan policy output is not accepted")
    for field in ("started_at", "finished_at"):
        if not isinstance(output[field], str) or not output[field]:
            raise ValueError("Acceptance Plan policy timestamp is invalid")
    duration = output["duration_seconds"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("Acceptance Plan policy duration is invalid")
    return output
