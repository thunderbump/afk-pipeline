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
CAPABILITY_POLICY = "contract-valid-capability-plan-v2"
CAPABILITY_DIRECT_POLICY = "pipeline-compatible-capability-direct-v2"
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


class PlanNeedsClarification(ValueError):
    """Raised when a v2 Plan retains unresolved routing ambiguity."""


class RoutingNeedsCallerAgent(ValueError):
    """Raised when direct work belongs with the agent that invoked AFK."""


class RoutingNeedsOutsideHelp(ValueError):
    """Raised when direct work needs a capability outside the agent system."""

    def __init__(self, reason: str):
        super().__init__(f"routing needs outside help: {reason}")
        self.reason = reason


def plan_policy(version: int) -> str:
    return POLICY if version == 1 else CAPABILITY_POLICY


def direct_policy(version: int) -> str:
    return DIRECT_POLICY if version == 1 else CAPABILITY_DIRECT_POLICY


def accept_plan(planner_input: object, plan: object) -> dict[str, object]:
    request = validate_input(planner_input)
    validated_plan = validate_plan(request, plan)
    if validated_plan["status"] != "proposed" or validated_plan["ambiguities"]:
        if request["schema_version"] == 2:
            raise PlanNeedsClarification("plan needs clarification")
        raise PlanNeedsHuman("plan needs human interpretation")
    body = {
        "schema_version": request["schema_version"],
        "status": "accepted",
        "policy": plan_policy(request["schema_version"]),
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
    if validated["status"] != "proposed" or validated["ambiguities"]:
        if request["schema_version"] == 2:
            raise PlanNeedsClarification("routing needs clarification")
        raise RoutingNeedsHuman("routing cannot use the direct pipeline path")
    if request["schema_version"] == 2:
        executors = {route["executor"] for route in validated["routes"]}
        if "outside_help" in executors:
            reasons = {
                route["outside_help_reason"]
                for route in validated["routes"]
                if route["executor"] == "outside_help"
            }
            if len(reasons) != 1:
                raise RoutingNeedsOutsideHelp("multiple_outside_capabilities")
            raise RoutingNeedsOutsideHelp(reasons.pop())
        if "caller_agent" in executors:
            raise RoutingNeedsCallerAgent(
                "caller-agent work must be represented as an accepted child Plan"
            )
    if not direct_pipeline_compatible(request, validated["routes"]):
        raise RoutingNeedsHuman("routing cannot use the direct pipeline path")
    body = {
        "schema_version": request["schema_version"],
        "status": "accepted",
        "policy": direct_policy(request["schema_version"]),
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
    expected_plan_policy = plan_policy(request["schema_version"])
    expected_direct_policy = direct_policy(request["schema_version"])
    accepted_plan = (
        output["decision"] == "accepted" and output["policy"] == expected_plan_policy
    )
    accepted_direct = (
        output["decision"] == "direct" and output["policy"] == expected_direct_policy
    )
    if (
        output["schema_version"] != request["schema_version"]
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
