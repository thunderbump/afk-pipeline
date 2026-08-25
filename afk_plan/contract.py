"""Validate planner inputs and turn inference proposals into canonical plans."""

import hashlib
import json
import re
from datetime import datetime, timedelta

from afk_config import validate_inference_setting

MAX_TEXT = 32 * 1024
MAX_CRITERIA = 128
MAX_CHILDREN = 64
MAX_PROJECTS = 64
MAX_ROUTES = 16
DOMAIN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
EXECUTIONS = {"agent", "human", "external"}
EXECUTORS = {"afk_run", "caller_agent", "outside_help"}
OUTSIDE_HELP_REASONS = {
    "missing_authority",
    "missing_credentials",
    "human_judgment",
    "physical_action",
    "unavailable_system",
}
EVIDENCE_ROUTES = {
    "pipeline_run",
    "repository_check",
    "external_check",
    "human_attestation",
}
PHASES = {"implementation", "closure"}


def validate_input(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("input must be an object")
    expected = {"schema_version", "parent", "catalog", "timeout_seconds"}
    request = object_with_keys(
        value, expected | ({"inference"} if "inference" in value else set()), "input"
    )
    if "inference" in request:
        request["inference"] = validate_inference_setting(request["inference"])
    if request["schema_version"] not in {1, 2}:
        raise ValueError("input schema_version must be 1 or 2")
    timeout = request["timeout_seconds"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise ValueError("timeout_seconds must be an integer from 1 through 3600")
    request["parent"] = validate_parent(request["parent"])
    request["catalog"] = validate_catalog(request["catalog"], request["schema_version"])
    project_labels = [
        label.removeprefix("project:")
        for label in request["parent"]["labels"]
        if label.startswith("project:")
    ]
    if len(project_labels) != 1:
        raise ValueError("parent must have exactly one project label")
    catalog_projects = {project["slug"] for project in request["catalog"]["projects"]}
    if project_labels[0] not in catalog_projects:
        raise ValueError("parent project label must exist in the catalog")
    return request


def validate_parent(value: object) -> dict[str, object]:
    parent = object_with_keys(
        value,
        {"id", "title", "description", "acceptance_criteria", "labels"},
        "parent",
    )
    bounded_text(parent["id"], "parent.id", 256)
    bounded_text(parent["title"], "parent.title", 1024)
    bounded_text(parent["description"], "parent.description", MAX_TEXT, empty=True)
    bounded_text(parent["acceptance_criteria"], "parent.acceptance_criteria", MAX_TEXT)
    parent["labels"] = string_list(parent["labels"], "parent.labels", 64, 256)
    return parent


def validate_catalog(value: object, version: int = 1) -> dict[str, object]:
    catalog = object_with_keys(value, {"schema_version", "projects"}, "catalog")
    if catalog["schema_version"] != version:
        raise ValueError("catalog schema_version must match its input")
    projects = catalog["projects"]
    if not isinstance(projects, list) or not 1 <= len(projects) <= MAX_PROJECTS:
        raise ValueError(
            f"catalog.projects must contain 1 through {MAX_PROJECTS} items"
        )
    slugs = set()
    accepted = []
    for index, project_value in enumerate(projects):
        project = object_with_keys(
            project_value, {"slug", "routes"}, f"catalog.projects[{index}]"
        )
        slug = project["slug"]
        if not isinstance(slug, str) or DOMAIN_ID_PATTERN.fullmatch(slug) is None:
            raise ValueError(f"catalog.projects[{index}].slug is invalid")
        if slug in slugs:
            raise ValueError("catalog project slugs must be unique")
        slugs.add(slug)
        routes = project["routes"]
        if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_ROUTES:
            raise ValueError(
                f"catalog project routes must contain 1 through {MAX_ROUTES} items"
            )
        route_keys = set()
        accepted_routes = []
        for route_index, route_value in enumerate(routes):
            route = dict_value(
                route_value, f"catalog.projects[{index}].routes[{route_index}]"
            )
            execution_name = execution_field(version)
            required = {"owner", execution_name, "evidence_route", "phases"}
            valid_fields = set(route) == required or (
                version == 2 and set(route) == required | {"outside_help_reason"}
            )
            if not valid_fields:
                raise ValueError(
                    f"catalog.projects[{index}].routes[{route_index}] has invalid fields"
                )
            bounded_text(route["owner"], "catalog owner", 256)
            validate_executor(route, version, "catalog")
            enum(route["evidence_route"], EVIDENCE_ROUTES, "catalog evidence_route")
            phases = string_list(route["phases"], "catalog phases", len(PHASES), 32)
            if not set(phases) <= PHASES:
                raise ValueError("catalog phase is invalid")
            route_key = (
                route["owner"],
                route[execution_name],
                route["evidence_route"],
                route.get("outside_help_reason"),
            )
            if route_key in route_keys:
                raise ValueError("catalog routes must be unique per project")
            route_keys.add(route_key)
            route["phases"] = phases
            accepted_routes.append(route)
        project["routes"] = accepted_routes
        accepted.append(project)
    catalog["projects"] = accepted
    return catalog


def build_plan(request: dict[str, object], proposal: object) -> dict[str, object]:
    proposal = validate_proposal(request, proposal)
    children = []
    for child in proposal["children"]:
        children.append(
            {
                **child,
                "readiness": readiness_for_execution(
                    child[execution_field(request["schema_version"])],
                    request["schema_version"],
                ),
            }
        )
    body = {
        "schema_version": request["schema_version"],
        "status": routing_status(request["schema_version"], proposal["ambiguities"]),
        "parent": {
            "id": request["parent"]["id"],
            "sha256": digest(request["parent"]),
        },
        "catalog_sha256": digest(request["catalog"]),
        "criteria": proposal["criteria"],
        "children": children,
        "ambiguities": proposal["ambiguities"],
        "authorization": None,
    }
    return {**body, "plan_sha256": digest(body)}


def build_routing(
    request: dict[str, object], proposal_value: object
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Build one visible routing decision and an optional existing Plan."""
    proposal = object_with_keys(
        proposal_value,
        {
            "schema_version",
            "decision",
            "criteria",
            "direct_routes",
            "children",
            "ambiguities",
        },
        "routing proposal",
    )
    if proposal["schema_version"] != request["schema_version"]:
        raise ValueError("routing proposal schema_version must match its input")
    enum(proposal["decision"], {"direct", "decompose"}, "routing decision")
    if proposal["decision"] == "direct":
        if proposal["children"] != []:
            raise ValueError("direct routing must not contain children")
        criteria = validate_criteria(request, proposal["criteria"])
        ambiguities = validate_ambiguities(proposal["ambiguities"])
        routes = validate_direct_routes(request, proposal["direct_routes"], criteria)
        plan = None
        status = routing_status(request["schema_version"], ambiguities)
    else:
        if proposal["direct_routes"] != []:
            raise ValueError("decompose routing must not contain direct routes")
        plan = build_plan(
            request,
            {
                "schema_version": request["schema_version"],
                "criteria": proposal["criteria"],
                "children": proposal["children"],
                "ambiguities": proposal["ambiguities"],
            },
        )
        criteria = plan["criteria"]
        ambiguities = plan["ambiguities"]
        routes = child_routes(plan["children"], criteria)
        if direct_pipeline_compatible(request, routes):
            raise ValueError(
                "decompose routing requires a cross-project or lifecycle boundary"
            )
        status = plan["status"]
    body = {
        "schema_version": request["schema_version"],
        "status": status,
        "decision": proposal["decision"],
        "parent": {
            "id": request["parent"]["id"],
            "sha256": digest(request["parent"]),
        },
        "catalog_sha256": digest(request["catalog"]),
        "criteria": criteria,
        "routes": routes,
        "ambiguities": ambiguities,
    }
    return {**body, "routing_sha256": digest(body)}, plan


def validate_direct_routing(
    request: dict[str, object], value: object
) -> dict[str, object]:
    routing = object_with_keys(
        value,
        {
            "schema_version",
            "status",
            "decision",
            "parent",
            "catalog_sha256",
            "criteria",
            "routes",
            "ambiguities",
            "routing_sha256",
        },
        "direct routing",
    )
    if routing["decision"] != "direct" or not isinstance(routing["routes"], list):
        raise ValueError("routing is not direct")
    direct_routes = []
    for route_value in routing["routes"]:
        route = dict_value(route_value, "direct route")
        if route.get("target") != {
            "kind": "source",
            "id": request["parent"]["id"],
        }:
            raise ValueError("direct route target must be the source Bead")
        direct_routes.append(
            {key: item for key, item in route.items() if key != "target"}
        )
    rebuilt, plan = build_routing(
        request,
        {
            "schema_version": request["schema_version"],
            "decision": "direct",
            "criteria": routing["criteria"],
            "direct_routes": direct_routes,
            "children": [],
            "ambiguities": routing["ambiguities"],
        },
    )
    if plan is not None or rebuilt != routing:
        raise ValueError("direct routing does not match its deterministic contract")
    return routing


def validate_plan(request: dict[str, object], value: object) -> dict[str, object]:
    plan = object_with_keys(
        value,
        {
            "schema_version",
            "status",
            "parent",
            "catalog_sha256",
            "criteria",
            "children",
            "ambiguities",
            "authorization",
            "plan_sha256",
        },
        "plan",
    )
    proposal_children = []
    for index, child_value in enumerate(
        plan["children"] if isinstance(plan["children"], list) else []
    ):
        child = dict_value(child_value, f"plan.children[{index}]")
        if "readiness" not in child:
            raise ValueError("plan child readiness is missing")
        expected_readiness = readiness_for_execution(
            child.get(execution_field(request["schema_version"])),
            request["schema_version"],
        )
        if child["readiness"] != expected_readiness:
            raise ValueError("plan child readiness does not match execution")
        proposal_children.append(
            {key: item for key, item in child.items() if key != "readiness"}
        )
    rebuilt = build_plan(
        request,
        {
            "schema_version": plan["schema_version"],
            "criteria": plan["criteria"],
            "children": proposal_children,
            "ambiguities": plan["ambiguities"],
        },
    )
    if rebuilt != plan:
        raise ValueError("plan does not match its deterministic contract")
    return plan


def validate_proposal(request: dict[str, object], value: object) -> dict[str, object]:
    proposal = object_with_keys(
        value, {"schema_version", "criteria", "children", "ambiguities"}, "proposal"
    )
    if proposal["schema_version"] != request["schema_version"]:
        raise ValueError("proposal schema_version must match its input")
    accepted_criteria = validate_criteria(request, proposal["criteria"])
    children = validate_children(request, proposal["children"], accepted_criteria)
    ambiguities = validate_ambiguities(proposal["ambiguities"])
    proposal["criteria"] = accepted_criteria
    proposal["children"] = children
    proposal["ambiguities"] = ambiguities
    return proposal


def validate_criteria(request, criteria):
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= MAX_CRITERIA:
        raise ValueError(
            f"proposal criteria must contain 1 through {MAX_CRITERIA} items"
        )
    accepted_criteria = []
    for index, criterion_value in enumerate(criteria, start=1):
        criterion = object_with_keys(
            criterion_value,
            {"id", "source_text", "statement"},
            f"criterion {index}",
        )
        if criterion["id"] != f"criterion-{index}":
            raise ValueError("criterion ids must be contiguous and one-based")
        bounded_text(criterion["source_text"], "criterion source_text", MAX_TEXT)
        bounded_text(criterion["statement"], "criterion statement", 2048)
        accepted_criteria.append(criterion)
    source = normalize(" ".join(item["source_text"] for item in accepted_criteria))
    if source != normalize(request["parent"]["acceptance_criteria"]):
        raise ValueError(
            "criterion source_text values must exactly cover acceptance_criteria"
        )

    return accepted_criteria


def validate_ambiguities(value):
    return string_list(value, "proposal ambiguities", 32, 2048)


def validate_direct_routes(request, values, criteria):
    if not isinstance(values, list) or len(values) != len(criteria):
        raise ValueError("direct routes must cover every criterion exactly once")
    projects = {project["slug"]: project for project in request["catalog"]["projects"]}
    accepted = []
    for index, (value, criterion) in enumerate(zip(values, criteria, strict=True)):
        route = dict_value(value, f"direct route {index + 1}")
        execution_name = execution_field(request["schema_version"])
        required = {
            "criterion",
            "project",
            "owner",
            "phase",
            execution_name,
            "evidence_route",
        }
        valid_fields = set(route) == required or (
            request["schema_version"] == 2
            and set(route) == required | {"outside_help_reason"}
        )
        if not valid_fields:
            raise ValueError(f"direct route {index + 1} has invalid fields")
        if route["criterion"] != criterion["id"]:
            raise ValueError("direct routes must follow criterion order")
        if route["project"] not in projects:
            raise ValueError("direct route project is not in the catalog")
        bounded_text(route["owner"], "direct route owner", 256)
        enum(route["phase"], PHASES, "direct route phase")
        validate_executor(route, request["schema_version"], "direct route")
        enum(route["evidence_route"], EVIDENCE_ROUTES, "direct route evidence_route")
        if not catalog_allows(projects[route["project"]], route):
            raise ValueError("direct route does not match a catalog route")
        accepted.append(
            {
                "criterion": route["criterion"],
                "target": {"kind": "source", "id": request["parent"]["id"]},
                "project": route["project"],
                "owner": route["owner"],
                "phase": route["phase"],
                execution_name: route[execution_name],
                "evidence_route": route["evidence_route"],
                **(
                    {"outside_help_reason": route["outside_help_reason"]}
                    if "outside_help_reason" in route
                    else {}
                ),
            }
        )
    return accepted


def child_routes(children, criteria):
    by_criterion = {
        criterion: child for child in children for criterion in child["criteria"]
    }
    return [
        {
            "criterion": criterion["id"],
            "target": {
                "kind": "child",
                "id": by_criterion[criterion["id"]]["local_id"],
            },
            "project": by_criterion[criterion["id"]]["project"],
            "owner": by_criterion[criterion["id"]]["owner"],
            "phase": by_criterion[criterion["id"]]["phase"],
            **{
                key: by_criterion[criterion["id"]][key]
                for key in ("execution", "executor")
                if key in by_criterion[criterion["id"]]
            },
            "evidence_route": by_criterion[criterion["id"]]["evidence_route"],
            **(
                {
                    "outside_help_reason": by_criterion[criterion["id"]][
                        "outside_help_reason"
                    ]
                }
                if "outside_help_reason" in by_criterion[criterion["id"]]
                else {}
            ),
        }
        for criterion in criteria
    ]


def direct_pipeline_compatible(request, routes):
    source_project = next(
        label.removeprefix("project:")
        for label in request["parent"]["labels"]
        if label.startswith("project:")
    )
    return all(
        route["project"] == source_project
        and route[execution_field(request["schema_version"])]
        == ("agent" if request["schema_version"] == 1 else "afk_run")
        and route["phase"] == "implementation"
        and route["evidence_route"] in {"pipeline_run", "repository_check"}
        for route in routes
    )


def validate_children(request, values, criteria):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_CHILDREN:
        raise ValueError(
            f"proposal children must contain 1 through {MAX_CHILDREN} items"
        )
    version = request.get("schema_version", 1)
    projects = {project["slug"]: project for project in request["catalog"]["projects"]}
    criterion_ids = {criterion["id"] for criterion in criteria}
    child_ids = set()
    accepted = []
    assigned = []
    for index, value in enumerate(values):
        child = dict_value(value, f"child {index + 1}")
        execution_name = execution_field(version)
        required = {
            "local_id",
            "title",
            "objective",
            "criteria",
            "project",
            "owner",
            "phase",
            execution_name,
            "evidence_route",
            "depends_on",
        }
        allowed = (
            required | {"handoff"}
            if version == 1
            else required | {"outside_help_reason"}
        )
        if set(child) != required and set(child) != allowed:
            raise ValueError(f"child {index + 1} has invalid fields")
        local_id = child["local_id"]
        if (
            not isinstance(local_id, str)
            or DOMAIN_ID_PATTERN.fullmatch(local_id) is None
        ):
            raise ValueError("child local_id is invalid")
        if local_id in child_ids:
            raise ValueError("child local_ids must be unique")
        child_ids.add(local_id)
        bounded_text(child["title"], "child title", 1024)
        bounded_text(child["objective"], "child objective", 4096)
        child["criteria"] = string_list(
            child["criteria"], "child criteria", MAX_CRITERIA, 256
        )
        if not child["criteria"] or not set(child["criteria"]) <= criterion_ids:
            raise ValueError("child criteria must reference known criteria")
        assigned.extend(child["criteria"])
        if child["project"] not in projects:
            raise ValueError("child project is not in the catalog")
        bounded_text(child["owner"], "child owner", 256)
        enum(child["phase"], PHASES, "child phase")
        validate_executor(child, version, "child")
        enum(child["evidence_route"], EVIDENCE_ROUTES, "child evidence_route")
        if not catalog_allows(projects[child["project"]], child):
            raise ValueError("child does not match a catalog route")
        child["depends_on"] = string_list(
            child["depends_on"], "child depends_on", MAX_CHILDREN, 128
        )
        if version == 1:
            if child["execution"] == "agent":
                if "handoff" in child:
                    raise ValueError("agent child must not have a handoff")
            else:
                child["handoff"] = validate_handoff(
                    child.get("handoff"), child["owner"], child["evidence_route"]
                )
        accepted.append(child)
    if sorted(assigned) != sorted(criterion_ids):
        raise ValueError("every criterion must be assigned exactly once")
    validate_graph(accepted)
    return accepted


def validate_handoff(value, owner, evidence_route):
    handoff = object_with_keys(
        value, {"authority", "subject_fields", "completion_record"}, "handoff"
    )
    bounded_text(handoff["authority"], "handoff authority", 1024)
    if handoff["authority"] != owner:
        raise ValueError("handoff authority must match the trusted child owner")
    fields = string_list(handoff["subject_fields"], "handoff subject_fields", 8, 64)
    if not fields or not set(fields) <= {"commit", "environment"}:
        raise ValueError("handoff subject_fields are invalid")
    if handoff["completion_record"] != evidence_route or evidence_route not in {
        "human_attestation",
        "external_check",
    }:
        raise ValueError("handoff completion_record must match its evidence route")
    handoff["subject_fields"] = fields
    return handoff


def validate_graph(children):
    by_id = {child["local_id"]: child for child in children}
    for child in children:
        if child["local_id"] in child["depends_on"] or not set(
            child["depends_on"]
        ) <= set(by_id):
            raise ValueError("child dependencies must reference other children")
    visiting = set()
    visited = set()

    def visit(local_id):
        if local_id in visiting:
            raise ValueError("child dependency graph contains a cycle")
        if local_id in visited:
            return
        visiting.add(local_id)
        for dependency in by_id[local_id]["depends_on"]:
            visit(dependency)
        visiting.remove(local_id)
        visited.add(local_id)

    for local_id in by_id:
        visit(local_id)

    implementations = {
        child["local_id"] for child in children if child["phase"] == "implementation"
    }

    def ancestors(local_id):
        direct = set(by_id[local_id]["depends_on"])
        return direct | {
            item for dependency in direct for item in ancestors(dependency)
        }

    if implementations:
        for child in children:
            if child["phase"] == "closure" and not (
                ancestors(child["local_id"]) & implementations
            ):
                raise ValueError("closure children must follow implementation")
            if child["phase"] == "implementation" and any(
                by_id[ancestor]["phase"] == "closure"
                for ancestor in ancestors(child["local_id"])
            ):
                raise ValueError("implementation children must not follow closure")


def catalog_allows(project, child):
    execution_name = "executor" if "executor" in child else "execution"
    return any(
        route["owner"] == child["owner"]
        and route[execution_name] == child[execution_name]
        and route["evidence_route"] == child["evidence_route"]
        and route.get("outside_help_reason") == child.get("outside_help_reason")
        and child["phase"] in route["phases"]
        for route in project["routes"]
    )


def readiness_for_execution(execution: object, version: int = 1) -> str:
    if version == 2:
        return (
            "ready-for-agent"
            if execution in {"afk_run", "caller_agent"}
            else "ready-for-human"
        )
    return "ready-for-agent" if execution == "agent" else "ready-for-human"


def execution_field(version: int) -> str:
    return "execution" if version == 1 else "executor"


def validate_executor(route: dict[str, object], version: int, name: str) -> None:
    field = execution_field(version)
    choices = EXECUTIONS if version == 1 else EXECUTORS
    enum(route.get(field), choices, f"{name} {field}")
    reason = route.get("outside_help_reason")
    if version == 2 and route[field] == "outside_help":
        enum(reason, OUTSIDE_HELP_REASONS, f"{name} outside_help_reason")
    elif reason is not None:
        raise ValueError(f"{name} outside_help_reason requires outside_help")


def routing_status(version: int, ambiguities: list[str]) -> str:
    if not ambiguities:
        return "proposed"
    return "needs_human" if version == 1 else "needs_clarification"


def validate_planner_output(planner_input: object, value: object) -> dict[str, object]:
    """Revalidate one complete successful Planner envelope and its route union."""
    request = validate_input(planner_input)
    output = object_with_keys(
        value,
        {
            "schema_version",
            "outcome",
            "source",
            "started_at",
            "finished_at",
            "duration_seconds",
            "process",
            "agent",
            "planner",
            "routing",
            "plan",
            "error_category",
            "artifacts",
        },
        "Planner output",
    )
    planner_model = request.get("inference", {"model": "gpt-5.6-luna"})["model"]
    if (
        output["schema_version"] != 1
        or output["outcome"] != "completed"
        or output["source"] != {"kind": "bead", "id": request["parent"]["id"]}
        or output["process"] != {"exit_code": 0, "signal": None}
        or output["agent"] != {"status": "completed"}
        or output["planner"]
        != {
            "kind": "inference",
            "provider": "openai-codex",
            "model": planner_model,
            "status": "completed",
        }
        or output["error_category"] is not None
        or output["artifacts"] != {"events": "events.jsonl", "stderr": "stderr.log"}
    ):
        raise ValueError("Planner output is not a successful canonical result")
    utc_timestamp(output["started_at"], "Planner started_at")
    utc_timestamp(output["finished_at"], "Planner finished_at")
    duration = output["duration_seconds"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ValueError("Planner duration_seconds is invalid")
    routing = output["routing"]
    if isinstance(routing, dict) and routing.get("decision") == "direct":
        validate_direct_routing(request, routing)
        if output["plan"] is not None:
            raise ValueError("direct Planner output must not contain a Plan")
    else:
        plan = validate_plan(request, output["plan"])
        children = [
            {key: item for key, item in child.items() if key != "readiness"}
            for child in plan["children"]
        ]
        rebuilt_routing, rebuilt_plan = build_routing(
            request,
            {
                "schema_version": request["schema_version"],
                "decision": "decompose",
                "criteria": plan["criteria"],
                "direct_routes": [],
                "children": children,
                "ambiguities": plan["ambiguities"],
            },
        )
        if routing != rebuilt_routing or plan != rebuilt_plan:
            raise ValueError("decomposed Planner output is not canonical")
    return output


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize(value: str) -> str:
    return " ".join(value.split())


def object_with_keys(value, keys, name):
    result = dict_value(value, name)
    if set(result) != keys:
        raise ValueError(f"{name} must contain exactly {sorted(keys)}")
    return result


def dict_value(value, name):
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def bounded_text(value, name, maximum, empty=False):
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or len(value.encode()) > maximum
    ):
        raise ValueError(f"{name} must be bounded text")
    return value


def string_list(value, name, maximum_items, maximum_bytes):
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{name} must be a bounded array")
    if not all(
        isinstance(item, str) and item.strip() and len(item.encode()) <= maximum_bytes
        for item in value
    ):
        raise ValueError(f"{name} must contain bounded strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must contain unique values")
    return list(value)


def utc_timestamp(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must identify a UTC instant")
    return value


def enum(value, choices, name):
    if value not in choices:
        raise ValueError(f"{name} is invalid")
