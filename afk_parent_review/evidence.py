"""Validate typed terminal evidence before aggregate parent inference."""

import json
from datetime import datetime
from pathlib import Path

from afk_change.contract import validate_change_output
from afk_coordinate.contract import validate_output as validate_coordinator_output
from afk_plan.contract import utc_timestamp


def validate_terminal_evidence(terminal, child, child_id, completion):
    expected_kind = (
        child["evidence_route"]
        if child["evidence_route"] in {"pipeline_run", "repository_check"}
        else "completion_record"
    )
    if terminal["kind"] != expected_kind:
        raise ValueError("terminal evidence kind does not match the accepted child")
    if expected_kind == "completion_record":
        return {"kind": "completion_record", "status": "validated"}
    if expected_kind == "pipeline_run":
        return validate_pipeline_run(
            terminal["directory"], child_id, completion["record"]
        )
    return validate_repository_check(terminal["directory"], completion["record"])


def validate_pipeline_run(directory: Path, child_id: str, record):
    bead = json.loads((directory / "bead.json").read_text())
    assignment = json.loads((directory / "assignment.json").read_text())
    preparation = json.loads((directory / "preparation.json").read_text())
    if (
        not isinstance(bead, dict)
        or bead.get("schema_version") != 1
        or bead.get("source") != {"kind": "bead", "id": child_id}
        or not isinstance(assignment, dict)
        or assignment.get("schema_version") != 1
        or assignment.get("source") != {"kind": "bead", "id": child_id}
        or not isinstance(preparation, dict)
        or preparation.get("schema_version") != 1
        or preparation.get("bead") != {"id": child_id}
        or preparation.get("preparation_status") != "prepared"
    ):
        raise ValueError("pipeline terminal evidence does not match the child")
    coordinator = preparation.get("coordinator")
    timestamps = preparation.get("timestamps")
    if (
        not isinstance(coordinator, dict)
        or coordinator.get("status") != "completed"
        or coordinator.get("exit_code") != 0
        or coordinator.get("outcome") != "completed"
        or coordinator.get("decision") != "stop"
        or coordinator.get("result") != "coordinator/output.json"
        or not isinstance(timestamps, dict)
    ):
        raise ValueError("pipeline Run is not terminally successful")
    finished_at = utc_timestamp(timestamps.get("finished_at"), "pipeline finished_at")
    if parse_time(record["accepted_at"]) < parse_time(finished_at):
        raise ValueError("Completion Record predates its pipeline evidence")
    coordinator_directory = directory / "coordinator"
    output = validate_coordinator_output(
        json.loads((coordinator_directory / "output.json").read_text())
    )
    if output["outcome"] != "completed" or output["decision"] != "stop":
        raise ValueError("pipeline Coordinator did not complete with stop")
    change_record = next(
        (
            item
            for item in reversed(output["history"])
            if item["component"] == "change" and item["outcome"] == "completed"
        ),
        None,
    )
    if change_record is None:
        raise ValueError("pipeline terminal evidence has no committed change")
    change = validate_change_output(
        json.loads(
            (
                coordinator_directory / change_record["directory"] / "output.json"
            ).read_text()
        )
    )
    if record["subject"].get("commit") != change["repository"]["after"]["head"]:
        raise ValueError(
            "pipeline terminal commit does not match the Completion Record"
        )
    return {
        "kind": "pipeline_run",
        "status": "completed",
        "decision": "stop",
        "commit": change["repository"]["after"]["head"],
    }


def validate_repository_check(directory: Path, record):
    request = json.loads((directory / "input.json").read_text())
    output = json.loads((directory / "output.json").read_text())
    repository = output.get("repository") if isinstance(output, dict) else None
    process = output.get("process") if isinstance(output, dict) else None
    if (
        not isinstance(request, dict)
        or request.get("schema_version") != 1
        or not isinstance(repository, dict)
        or not isinstance(process, dict)
        or output.get("schema_version") != 1
        or output.get("outcome") != "passed"
        or process.get("exit_code") != 0
        or process.get("signal") is not None
        or process.get("error") is not None
        or repository.get("head_changed") is not False
        or repository.get("before") != repository.get("after")
    ):
        raise ValueError("repository check is not terminally successful")
    state = repository["after"]
    head = state.get("head") if isinstance(state, dict) else None
    if not isinstance(head, str) or record["subject"].get("commit") != head:
        raise ValueError("repository check commit does not match the Completion Record")
    if state.get("dirty") is not False or state.get("status") != []:
        raise ValueError("repository check did not preserve a clean repository")
    finished_at = utc_timestamp(
        output.get("finished_at"), "repository check finished_at"
    )
    if parse_time(record["accepted_at"]) < parse_time(finished_at):
        raise ValueError("Completion Record predates its repository check")
    return {"kind": "repository_check", "status": "passed", "commit": head}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
