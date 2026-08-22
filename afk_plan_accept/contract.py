"""Accept structurally valid, unambiguous Acceptance Plans without I/O."""

from afk_plan.contract import digest, validate_input, validate_plan

POLICY = "contract-valid-proposed-v1"


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
