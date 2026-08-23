"""Accept structurally valid, unambiguous Acceptance Plans without I/O."""

from afk_plan.contract import (
    digest,
    direct_pipeline_compatible,
    validate_direct_routing,
    validate_input,
    validate_plan,
)

POLICY = "contract-valid-proposed-v1"
DIRECT_POLICY = "pipeline-compatible-direct-v1"
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


class RoutingNeedsHuman(ValueError):
    """Raised when a direct route cannot safely use the existing pipeline."""


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


def accept_direct(planner_input: object, routing: object) -> dict[str, object]:
    request = validate_input(planner_input)
    validated = validate_direct_routing(request, routing)
    if (
        validated["status"] != "proposed"
        or validated["ambiguities"]
        or not direct_pipeline_compatible(request, validated["routes"])
    ):
        raise RoutingNeedsHuman("routing cannot use the direct pipeline path")
    body = {
        "schema_version": 1,
        "status": "accepted",
        "policy": DIRECT_POLICY,
        "basis": "pipeline_compatible_routes_only",
        "source": validated["parent"],
        "catalog_sha256": validated["catalog_sha256"],
        "routing_sha256": validated["routing_sha256"],
        "routing": validated,
    }
    return {**body, "acceptance_sha256": digest(body)}


def validate_accepted_output(planner_input: object, value: object) -> dict[str, object]:
    request = validate_input(planner_input)
    parent_id = request["parent"]["id"]
    if not isinstance(value, dict) or set(value) != OUTPUT_FIELDS:
        raise ValueError("Acceptance Plan policy output has an invalid shape")
    output = dict(value)
    accepted_plan = output["decision"] == "accepted" and output["policy"] == POLICY
    accepted_direct = (
        output["decision"] == "direct" and output["policy"] == DIRECT_POLICY
    )
    if (
        output["schema_version"] != 1
        or output["outcome"] != "completed"
        or not (accepted_plan or accepted_direct)
        or output["source"] != {"kind": "bead", "id": parent_id}
        or output["error_category"] is not None
        or output["artifacts"] != {"input": "input.json"}
    ):
        raise ValueError("Acceptance Routing policy output is not accepted")
    for field in ("started_at", "finished_at"):
        if not isinstance(output[field], str) or not output[field]:
            raise ValueError("Acceptance Routing policy timestamp is invalid")
    duration = output["duration_seconds"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("Acceptance Routing policy duration is invalid")
    acceptance = output["acceptance"]
    if not isinstance(acceptance, dict):
        raise TypeError("Acceptance Routing policy evidence is invalid")
    try:
        if accepted_direct:
            expected = accept_direct(request, acceptance["routing"])
        else:
            expected = accept_plan(request, acceptance["plan"])
    except (KeyError, TypeError) as error:
        raise ValueError("Acceptance Routing policy evidence is invalid") from error
    if acceptance != expected:
        raise ValueError("Acceptance Routing policy evidence does not match its input")
    return output
