"""Publish one immutable accepted Acceptance Plan as a Beads child graph."""

import json
import subprocess
import sys
import time
from pathlib import Path

from afk_plan_publish.contract import (
    child_acceptance,
    child_description,
    external_reference,
    load_accepted_plan,
)
from afk_runtime import progress, seal_json, timestamp, write_json

USAGE = "usage: python3 -m afk_plan_publish PUBLISH_JSON RESULT_DIRECTORY"


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        return 0
    if len(sys.argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    request = load_request(Path(sys.argv[1]))
    result = Path(sys.argv[2])
    validate_locations(result, request)
    started_at = timestamp()
    started = time.monotonic()
    planner_input, acceptance = load_accepted_plan(request["acceptance_directory"])
    adapter = Beads(request)
    parent = adapter.one("show", planner_input["parent"]["id"], "--json", "--readonly")
    validate_parent(parent, planner_input["parent"])
    plan = acceptance["plan"]
    existing = adapter.many("list", "--all", "--limit", "0", "--json", "--readonly")
    by_reference = {}
    for issue in existing:
        reference = issue.get("external_ref")
        if isinstance(reference, str) and reference.startswith(
            f"afk-plan:{plan['plan_sha256']}:"
        ):
            if reference in by_reference:
                raise ValueError("published child external_ref is duplicated")
            by_reference[reference] = issue

    result.mkdir()
    write_json(result / "input.json", request_for_output(request))
    mappings = []
    context = {
        "started_at": started_at,
        "started": started,
        "planner_input": planner_input,
        "acceptance": acceptance,
        "mappings": mappings,
    }
    by_local_id = {}
    try:
        for child in plan["children"]:
            reference = external_reference(plan["plan_sha256"], child["local_id"])
            issue = by_reference.get(reference)
            recorded = False
            if issue is None:
                issue = adapter.one(
                    "create",
                    child["title"],
                    "--description",
                    child_description(planner_input["parent"]["id"], plan, child),
                    "--acceptance",
                    child_acceptance(plan, child),
                    "--type",
                    "task",
                    "--priority",
                    "2",
                    "--labels",
                    f"project:{child['project']},{child['readiness']}",
                    "--parent",
                    planner_input["parent"]["id"],
                    "--external-ref",
                    reference,
                    "--no-inherit-labels",
                    "--json",
                )
                by_reference[reference] = issue
                bead_id = require_bead_id(issue)
                mappings.append({"local_id": child["local_id"], "bead_id": bead_id})
                recorded = True
            issue = adapter.one("show", issue["id"], "--json", "--readonly")
            bead_id = require_bead_id(issue)
            validate_child_identity(
                issue, planner_input["parent"]["id"], plan, child, reference
            )
            if child["executor"] == "outside_help":
                description = child_description(
                    planner_input["parent"]["id"], plan, child, bead_id
                )
                placeholder = child_description(
                    planner_input["parent"]["id"], plan, child
                )
                if issue.get("description") not in {description, placeholder}:
                    raise ValueError(
                        "published child description does not match the accepted plan"
                    )
                if issue.get("description") == placeholder:
                    adapter.many(
                        "update", bead_id, "--description", description, "--json"
                    )
                    issue["description"] = description
            validate_child(issue, planner_input["parent"]["id"], plan, child, reference)
            by_local_id[child["local_id"]] = issue
            if not recorded:
                mappings.append({"local_id": child["local_id"], "bead_id": bead_id})

        for child in plan["children"]:
            issue = by_local_id[child["local_id"]]
            dependencies = dependency_pairs(issue)
            for local_id in child["depends_on"]:
                dependency_id = by_local_id[local_id]["id"]
                if (dependency_id, "blocks") not in dependencies:
                    adapter.one(
                        "dep",
                        "add",
                        issue["id"],
                        dependency_id,
                        "--type",
                        "blocks",
                        "--json",
                    )
        observed = adapter.many(
            "show",
            *[item["bead_id"] for item in mappings],
            "--json",
            "--readonly",
        )
        observed_by_id = {item.get("id"): item for item in observed}
        if set(observed_by_id) != {item["bead_id"] for item in mappings}:
            raise ValueError("published child observation does not match the plan")
        for child in plan["children"]:
            issue = observed_by_id[by_local_id[child["local_id"]]["id"]]
            reference = external_reference(plan["plan_sha256"], child["local_id"])
            validate_child(issue, planner_input["parent"]["id"], plan, child, reference)
            dependencies = dependency_pairs(issue)
            required = {(planner_input["parent"]["id"], "parent-child")} | {
                (by_local_id[local_id]["id"], "blocks")
                for local_id in child["depends_on"]
            }
            controlled = {
                pair for pair in dependencies if pair[1] in {"blocks", "parent-child"}
            }
            if controlled != required:
                raise ValueError("published child dependencies do not match the plan")
    except KeyboardInterrupt:
        finish(result, adapter, context, "failed", "interrupted", "interrupted")
        return 130
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        finish(result, adapter, context, "failed", "failed", "beads_operation")
        progress(f"sealed failed child graph publication at {result / 'output.json'}")
        return 1

    decision = "published" if adapter.mutations else "replayed"
    finish(result, adapter, context, "completed", decision, None)
    progress(f"sealed {decision} child graph at {result / 'output.json'}")
    return 0


def finish(result, adapter, context, outcome, decision, error_category):
    write_json(result / "stdout.log.json", adapter.stdout)
    write_json(result / "stderr.log.json", adapter.stderr)
    seal_output(result, context, outcome, decision, error_category)


def seal_output(result, context, outcome, decision, error_category):
    started_at = context["started_at"]
    started = context["started"]
    planner_input = context["planner_input"]
    acceptance = context["acceptance"]
    plan = acceptance["plan"]
    output = {
        "schema_version": 1,
        "outcome": outcome,
        "decision": decision,
        "source": {"kind": "bead", "id": planner_input["parent"]["id"]},
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "acceptance_sha256": acceptance["acceptance_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "children": context["mappings"],
        "error_category": error_category,
        "artifacts": {
            "input": "input.json",
            "stdout": "stdout.log.json",
            "stderr": "stderr.log.json",
        },
    }
    seal_json(result / "output.json", output)


class Beads:
    def __init__(self, request):
        self.command = request["command"]
        self.cwd = request["beads_workspace"]
        self.timeout = request["timeout_seconds"]
        self.stdout = []
        self.stderr = []
        self.mutations = 0

    def run(self, *arguments):
        completed = subprocess.run(
            [*self.command, *arguments],
            cwd=self.cwd,
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        self.stdout.append({"arguments": list(arguments), "text": completed.stdout})
        self.stderr.append({"arguments": list(arguments), "text": completed.stderr})
        if completed.returncode != 0:
            raise RuntimeError(f"Beads command failed with exit {completed.returncode}")
        if arguments[0] in {"create", "update"} or arguments[:2] == ("dep", "add"):
            self.mutations += 1
        return json.loads(completed.stdout)

    def many(self, *arguments):
        value = self.run(*arguments)
        if not isinstance(value, list):
            raise TypeError("Beads command did not return an array")
        return value

    def one(self, *arguments):
        value = self.run(*arguments)
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("Beads command did not return one issue")
            return value[0]
        if not isinstance(value, dict):
            raise TypeError("Beads command did not return one issue")
        return value


def load_request(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "acceptance_directory",
        "beads_workspace",
        "command",
        "timeout_seconds",
    }:
        raise ValueError("publisher input has an invalid shape")
    if value["schema_version"] != 1:
        raise ValueError("publisher schema_version must be 1")
    for name in ("acceptance_directory", "beads_workspace"):
        path_value = Path(value[name])
        if not path_value.is_absolute() or not path_value.is_dir():
            raise ValueError(f"{name} must be an absolute existing directory")
        value[name] = path_value.resolve()
    command = value["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise ValueError("command must be a nonempty argv array")
    timeout = value["timeout_seconds"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 300
    ):
        raise ValueError("timeout_seconds must be an integer from 1 through 300")
    return value


def validate_locations(result, request):
    if result.exists() or not result.is_absolute() or not result.parent.is_dir():
        raise ValueError("result directory must be an absolute new path")
    result = result.parent.resolve() / result.name
    acceptance = request["acceptance_directory"]
    beads = request["beads_workspace"]
    if overlaps(acceptance, beads):
        raise ValueError("Acceptance evidence and Beads workspace must not overlap")
    if any(overlaps(result, protected) for protected in (acceptance, beads)):
        raise ValueError("result directory must be outside protected inputs")


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def request_for_output(request):
    return {
        **request,
        "acceptance_directory": str(request["acceptance_directory"]),
        "beads_workspace": str(request["beads_workspace"]),
    }


def validate_parent(issue, expected):
    for field in ("id", "title", "description", "acceptance_criteria"):
        if issue.get(field) != expected[field]:
            raise ValueError(f"parent Bead {field} does not match the frozen parent")
    if set(issue.get("labels", [])) != set(expected["labels"]):
        raise ValueError("parent Bead labels do not match the frozen parent")


def validate_child(issue, parent_id, plan, child, reference):
    validate_child_identity(issue, parent_id, plan, child, reference)
    if issue.get("description") != child_description(
        parent_id, plan, child, issue["id"]
    ):
        raise ValueError("published child description does not match the accepted plan")


def validate_child_identity(issue, parent_id, plan, child, reference):
    expected = {
        "title": child["title"],
        "acceptance_criteria": child_acceptance(plan, child),
        "external_ref": reference,
        "issue_type": "task",
        "priority": 2,
    }
    for field, value in expected.items():
        if issue.get(field) != value:
            raise ValueError(
                f"published child {field} does not match the accepted plan"
            )
    if set(issue.get("labels", [])) != {
        f"project:{child['project']}",
        child["readiness"],
    }:
        raise ValueError("published child labels do not match the accepted plan")
    dependencies = dependency_pairs(issue)
    parents = {pair for pair in dependencies if pair[1] == "parent-child"}
    if parents != {(parent_id, "parent-child")}:
        raise ValueError("published child is not attached to the parent")


def dependency_pairs(issue):
    return {
        (item.get("id"), item.get("dependency_type"))
        for item in issue.get("dependencies", [])
        if isinstance(item, dict)
    }


def require_bead_id(issue):
    bead_id = issue.get("id")
    if not isinstance(bead_id, str) or not bead_id:
        raise ValueError("Beads child response has no id")
    return bead_id


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"afk-plan-publish: {error}", file=sys.stderr)
        raise SystemExit(2)
