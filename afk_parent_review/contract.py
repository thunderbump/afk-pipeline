"""Deterministic evidence fan-in and Parent Acceptance Review result contract."""

import json
from pathlib import Path

from afk_complete.contract import load_result, validate_subject
from afk_parent_review.evidence import validate_terminal_evidence
from afk_plan.contract import (
    bounded_text,
    execution_field,
    object_with_keys,
    validate_children,
)
from afk_plan_publish.contract import load_accepted_plan, validate_published_output

DECISIONS = {"accepted", "incomplete"}
MAX_CHILDREN = 64


def load_request(value: object) -> dict[str, object]:
    request = object_with_keys(
        value,
        {
            "schema_version",
            "acceptance_directory",
            "publication_directory",
            "child_graph",
            "completions",
            "timeout_seconds",
        },
        "Parent Acceptance Review input",
    )
    if request["schema_version"] != 1:
        raise ValueError("Parent Acceptance Review schema_version must be 1")
    timeout = request["timeout_seconds"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 3600
    ):
        raise ValueError("timeout_seconds must be an integer from 1 through 3600")
    for name in ("acceptance_directory", "publication_directory"):
        directory = Path(request[name])
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(f"{name} must be an absolute existing directory")
        request[name] = directory.resolve()
    if overlaps(request["acceptance_directory"], request["publication_directory"]):
        raise ValueError("accepted Plan and publication evidence must not overlap")
    request["child_graph"] = graph_items(request["child_graph"])
    request["completions"] = completion_items(request["completions"])
    protected = [request["acceptance_directory"], request["publication_directory"]]
    for item in request["completions"]:
        directory = Path(item["directory"])
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(
                "completion directory must be an absolute existing directory"
            )
        directory = directory.resolve()
        if any(overlaps(directory, existing) for existing in protected):
            raise ValueError("source evidence directories must not overlap")
        protected.append(directory)
        item["directory"] = directory
        terminal = item["terminal"]
        if terminal["kind"] != "completion_record":
            terminal_directory = Path(terminal["directory"])
            if not terminal_directory.is_absolute() or not terminal_directory.is_dir():
                raise ValueError(
                    "terminal evidence directory must be an absolute existing directory"
                )
            terminal_directory = terminal_directory.resolve()
            if any(overlaps(terminal_directory, existing) for existing in protected):
                raise ValueError("source evidence directories must not overlap")
            protected.append(terminal_directory)
            terminal["directory"] = terminal_directory
    request["protected_directories"] = protected
    return request


def load_fan_in(request: dict[str, object]) -> dict[str, object]:
    planner_input, acceptance = load_accepted_plan(request["acceptance_directory"])
    publication = validate_published_output(
        json.loads((request["publication_directory"] / "output.json").read_text()),
        planner_input["parent"]["id"],
        acceptance,
    )
    plan = acceptance["plan"]
    executor_field = execution_field(plan["schema_version"])
    mappings = {item["local_id"]: item["bead_id"] for item in publication["children"]}
    validate_graph(
        request["child_graph"], planner_input["parent"]["id"], plan, mappings
    )
    completion_by_local = {item["local_id"]: item for item in request["completions"]}
    local_ids = {child["local_id"] for child in plan["children"]}
    if set(completion_by_local) != local_ids:
        raise ValueError("Completion results must exactly cover the accepted children")
    evidence = []
    for child in plan["children"]:
        child_id = mappings[child["local_id"]]
        output = load_result(
            completion_by_local[child["local_id"]]["directory"],
            child,
            child_id,
            acceptance["acceptance_sha256"],
            plan["plan_sha256"],
            request["acceptance_directory"],
            request["publication_directory"],
            completion_by_local[child["local_id"]]["current_subject"],
        )
        terminal = validate_terminal_evidence(
            completion_by_local[child["local_id"]]["terminal"],
            child,
            child_id,
            output,
        )
        evidence.append(
            {
                "local_id": child["local_id"],
                "child_id": child_id,
                "criteria": child["criteria"],
                executor_field: child[executor_field],
                "evidence_basis": output["evidence_basis"],
                "satisfies_criteria": output["satisfies_criteria"],
                "subject": output["record"]["subject"],
                "evidence": output["record"]["evidence"],
                "terminal": terminal,
            }
        )
    return {
        "schema_version": plan["schema_version"],
        "parent": planner_input["parent"],
        "plan_sha256": plan["plan_sha256"],
        "criteria": plan["criteria"],
        "catalog": planner_input["catalog"],
        "children": evidence,
    }


def validate_review(value: object, fan_in: dict[str, object]) -> dict[str, object]:
    review = object_with_keys(
        value, {"schema_version", "decision", "criteria", "gaps", "follow_up"}, "review"
    )
    if review["schema_version"] != 1 or review["decision"] not in DECISIONS:
        raise ValueError("review decision is invalid")
    planned = fan_in["criteria"]
    items = review["criteria"]
    if not isinstance(items, list) or len(items) != len(planned):
        raise ValueError("review criteria must exactly cover the parent criteria")
    accepted_items = []
    for expected, value_item in zip(planned, items, strict=True):
        item = object_with_keys(
            value_item, {"id", "decision", "rationale"}, "criterion decision"
        )
        if item["id"] != expected["id"] or item["decision"] not in DECISIONS:
            raise ValueError("criterion decision does not match the accepted Plan")
        bounded_text(item["rationale"], "criterion rationale", 2048)
        accepted_items.append(item)
    incomplete = [
        item["id"] for item in accepted_items if item["decision"] == "incomplete"
    ]
    gaps = review["gaps"]
    if not isinstance(gaps, list) or len(gaps) != len(incomplete):
        raise ValueError("review gaps must exactly cover incomplete criteria")
    accepted_gaps = []
    for criterion, gap_value in zip(incomplete, gaps, strict=True):
        gap = object_with_keys(gap_value, {"criterion", "summary"}, "review gap")
        if gap["criterion"] != criterion:
            raise ValueError("review gap does not match an incomplete criterion")
        bounded_text(gap["summary"], "review gap summary", 2048)
        accepted_gaps.append(gap)
    unsatisfied = {
        criterion
        for child in fan_in["children"]
        if not child["satisfies_criteria"]
        for criterion in child["criteria"]
    }
    if unsatisfied - set(incomplete):
        raise ValueError("non-satisfying child evidence cannot accept a criterion")
    if review["decision"] == "accepted":
        if incomplete or review["follow_up"] is not None:
            raise ValueError("accepted review cannot contain gaps or follow-up work")
    else:
        if not incomplete:
            raise ValueError("incomplete review must contain an incomplete criterion")
        review["follow_up"] = validate_follow_up(
            review["follow_up"], incomplete, fan_in
        )
    review["criteria"] = accepted_items
    review["gaps"] = accepted_gaps
    return review


def validate_follow_up(
    value: object, incomplete: list[str], fan_in: dict[str, object]
) -> dict[str, object]:
    incomplete_criteria = [
        criterion for criterion in fan_in["criteria"] if criterion["id"] in incomplete
    ]
    children = validate_children(
        {
            "schema_version": fan_in.get("schema_version", 1),
            "catalog": fan_in["catalog"],
        },
        [value],
        incomplete_criteria,
    )
    if children[0]["local_id"] in {child["local_id"] for child in fan_in["children"]}:
        raise ValueError("follow-up local_id must be new within the accepted Plan")
    return children[0]


def graph_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHILDREN:
        raise ValueError(f"child_graph must contain 1 through {MAX_CHILDREN} items")
    accepted = []
    ids = set()
    for graph_value in value:
        item = object_with_keys(
            graph_value, {"id", "status", "dependencies"}, "child graph item"
        )
        bounded_text(item["id"], "child graph id", 256)
        if item["id"] in ids or item["status"] != "closed":
            raise ValueError("child graph identities must be unique and closed")
        ids.add(item["id"])
        dependencies = item["dependencies"]
        if not isinstance(dependencies, list) or len(dependencies) > MAX_CHILDREN:
            raise ValueError("child graph dependencies exceed the accepted bound")
        seen = set()
        accepted_dependencies = []
        for dependency_value in dependencies:
            dependency = object_with_keys(
                dependency_value, {"id", "dependency_type"}, "child dependency"
            )
            bounded_text(dependency["id"], "child dependency id", 256)
            bounded_text(dependency["dependency_type"], "dependency type", 64)
            key = (dependency["id"], dependency["dependency_type"])
            if key in seen:
                raise ValueError("child graph dependencies must be unique")
            seen.add(key)
            accepted_dependencies.append(dependency)
        item["dependencies"] = accepted_dependencies
        accepted.append(item)
    return accepted


def completion_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHILDREN:
        raise ValueError(f"completions must contain 1 through {MAX_CHILDREN} items")
    accepted = []
    ids = set()
    for completion_value in value:
        item = object_with_keys(
            completion_value,
            {"local_id", "directory", "current_subject", "terminal"},
            "completion",
        )
        bounded_text(item["local_id"], "completion local_id", 128)
        if item["local_id"] in ids:
            raise ValueError("completion local_ids must be unique")
        ids.add(item["local_id"])
        item["current_subject"] = validate_subject(
            item["current_subject"], "completion current_subject"
        )
        terminal = item["terminal"]
        if not isinstance(terminal, dict) or terminal.get("kind") not in {
            "pipeline_run",
            "repository_check",
            "completion_record",
        }:
            raise ValueError("completion terminal evidence is invalid")
        expected_fields = (
            {"kind"}
            if terminal["kind"] == "completion_record"
            else {"kind", "directory"}
        )
        if set(terminal) != expected_fields:
            raise ValueError("completion terminal evidence has an invalid shape")
        item["terminal"] = dict(terminal)
        accepted.append(item)
    return accepted


def validate_graph(graph, parent_id, plan, mappings):
    by_id = {item["id"]: item for item in graph}
    if set(by_id) != set(mappings.values()):
        raise ValueError("child graph must exactly cover the publication")
    for child in plan["children"]:
        child_id = mappings[child["local_id"]]
        expected = {(parent_id, "parent-child")}
        expected.update((mappings[item], "blocks") for item in child["depends_on"])
        actual = {
            (item["id"], item["dependency_type"])
            for item in by_id[child_id]["dependencies"]
            if item["dependency_type"] in {"parent-child", "blocks"}
        }
        if actual != expected:
            raise ValueError("child graph topology does not match the accepted Plan")


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def request_for_output(request: dict[str, object]) -> dict[str, object]:
    return json_value(
        {key: value for key, value in request.items() if key != "protected_directories"}
    )


def json_value(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    return value
