"""Validate one scoped Child Completion Record without external mutation."""

import json
import sys
import time
from pathlib import Path

from afk_complete.contract import validate_record, validate_subject
from afk_plan_publish.contract import load_accepted_plan, validate_published_output
from afk_runtime import progress, seal_json, timestamp, write_json

USAGE = "usage: python3 -m afk_complete COMPLETION_JSON RESULT_DIRECTORY"


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
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
    progress("validating accepted Plan and child publication")
    planner_input, acceptance = load_accepted_plan(request["acceptance_directory"])
    publication = json.loads(
        (request["publication_directory"] / "output.json").read_text()
    )
    publication = validate_published_output(
        publication, planner_input["parent"]["id"], acceptance
    )
    plan = acceptance["plan"]
    by_local_id = {child["local_id"]: child for child in plan["children"]}
    mapping = next(
        (
            item
            for item in publication["children"]
            if item["bead_id"] == request["record"].get("child")
        ),
        None,
    )
    if mapping is None:
        raise ValueError("Completion Record child is not in the publication")
    child = by_local_id[mapping["local_id"]]
    record, basis, satisfies = validate_record(
        request["record"],
        child,
        mapping["bead_id"],
        plan["plan_sha256"],
        request["expected_subject"],
    )
    progress(f"Completion Record is {record['outcome']}")
    result.mkdir()
    write_json(result / "input.json", request_for_output(request))
    output = {
        "schema_version": 1,
        "outcome": "completed",
        "decision": record["outcome"],
        "source": {"kind": "bead", "id": mapping["bead_id"]},
        "started_at": started_at,
        "finished_at": timestamp(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "acceptance_sha256": acceptance["acceptance_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "local_id": mapping["local_id"],
        "criteria": child["criteria"],
        "evidence_basis": basis,
        "satisfies_criteria": satisfies,
        "record": record,
        "error_category": None,
        "artifacts": {"input": "input.json"},
    }
    seal_json(result / "output.json", output)
    progress(f"sealed Completion Record result at {result / 'output.json'}")
    return 0


def load_request(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "acceptance_directory",
        "publication_directory",
        "expected_subject",
        "record",
    }:
        raise ValueError("Completion Record input has an invalid shape")
    if value["schema_version"] != 1 or not isinstance(value["record"], dict):
        raise ValueError("Completion Record input is invalid")
    request = dict(value)
    for name in ("acceptance_directory", "publication_directory"):
        directory = Path(request[name])
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(f"{name} must be an absolute existing directory")
        request[name] = directory.resolve()
    request["expected_subject"] = validate_subject(
        request["expected_subject"], "expected_subject"
    )
    return request


def validate_locations(result: Path, request: dict[str, object]) -> None:
    if result.exists() or not result.is_absolute() or not result.parent.is_dir():
        raise ValueError("result directory must be an absolute new path")
    result = result.parent.resolve() / result.name
    acceptance = request["acceptance_directory"]
    publication = request["publication_directory"]
    if overlaps(acceptance, publication):
        raise ValueError("accepted Plan and publication evidence must not overlap")
    if any(overlaps(result, protected) for protected in (acceptance, publication)):
        raise ValueError("result directory must be outside source evidence")


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def request_for_output(request: dict[str, object]) -> dict[str, object]:
    return {
        **request,
        "acceptance_directory": str(request["acceptance_directory"]),
        "publication_directory": str(request["publication_directory"]),
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"afk-complete: {error}", file=sys.stderr)
        raise SystemExit(2)
