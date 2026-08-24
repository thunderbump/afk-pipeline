"""Deterministically export one sealed AFK Run as a portable bundle."""

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from afk_attempt.contract import validate_assignment
from afk_coordinate.contract import (
    COMPONENT_TOPOLOGY,
    validate_checkpoint,
    validate_component_output,
    validate_continuation,
    validate_output,
    validate_request,
)
from afk_plan.contract import validate_input as validate_plan_input
from afk_plan.contract import validate_planner_output
from afk_plan_accept.contract import validate_policy_output
from afk_preflight.contract import validate_input as validate_preflight_input
from afk_preflight.contract import validate_output as validate_preflight_output

SAFE_PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_JSON_BYTES = 1024 * 1024
MAX_INCLUDED_BYTES = 1024 * 1024
MAX_EVENTS_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_FILES = 128
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
V2_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
V2_MAX_BUNDLE_BYTES = 32 * 1024 * 1024
V2_MAX_ARTIFACT_NAME_BYTES = 255
MAX_MANIFEST_BYTES = 64 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
EVENT_TYPES = {
    "session",
    "agent_start",
    "agent_end",
    "agent_settled",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
}
ARTIFACTS = {
    "attempt": {"events", "stderr"},
    "validation": {"stdout", "stderr"},
    "change": set(),
    "review": {"diff", "events", "stderr"},
    "assessment": {"events", "stderr"},
    "iteration": set(),
    "response": {"events", "stderr"},
}
INCLUDED_NAMES = {"stdout": "stdout.txt", "stderr": "stderr.txt", "diff": "diff.patch"}
PRIVATE_KEY_TEXT = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
REDACTABLE_CREDENTIAL_TEXT = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(
        r"(?i)(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AZURE_CLIENT_SECRET|"
        r"GOOGLE_APPLICATION_CREDENTIALS)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)(?:authorization\s*:\s*)?(?:basic|bearer)\s+\S{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
)
SENSITIVE_TEXT = (*REDACTABLE_CREDENTIAL_TEXT, PRIVATE_KEY_TEXT)
HOST_PATH = re.compile(
    r"(?<![A-Za-z0-9.])/(?:home|tmp|var|etc|opt|root|srv|mnt|usr|run|"
    r"Users|private|Library|Volumes|Applications|System)/[^\s'\"`]+"
)
CREDENTIAL_OPTION = re.compile(
    r"(?i)^--(?:access[-_]?token|api[-_]?key|client[-_]?secret|password|secret|token)$"
)
CREDENTIAL_OPTION_VALUE = re.compile(
    r"(?i)^(--(?:access[-_]?token|api[-_]?key|client[-_]?secret|"
    r"password|secret|token))=(.*)$"
)
REDACTED_SECRET = "[redacted-secret]"
PUBLIC_PREFLIGHT_CLASSIFIER_KEY = "[sanitized-preflight-classifier-key]"


class ExportError(Exception):
    pass


class ExportUsageError(ExportError):
    pass


def export_run(
    source_path,
    destination_path,
    project=None,
    run_id=None,
    bead_id=None,
    schema_version=2,
    terminal_continuation=None,
):
    if schema_version not in {1, 2}:
        raise ExportUsageError("unsupported Publication Bundle schema")
    source_input = Path(source_path).absolute()
    destination_input = Path(destination_path).absolute()
    require_directory(source_input)
    if destination_input.exists() or destination_input.is_symlink():
        raise ExportError("bundle destination already exists")
    if not destination_input.parent.is_dir():
        raise ExportError("bundle destination parent is unavailable")
    source = source_input.resolve()
    destination = destination_input.parent.resolve() / destination_input.name
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ExportError("source and destination must not overlap")
    observed = (
        load_source(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
        )
        if schema_version == 1
        else load_source_v2(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
        )
    )
    record, payloads = (
        normalize_run(observed) if schema_version == 1 else normalize_run_v2(observed)
    )
    workflow = encode_json(record)
    if len(workflow) > MAX_INCLUDED_BYTES:
        raise ExportError("normalized Run exceeds bundle limits")
    payloads["workflow-run.json"] = workflow
    if len(payloads) > MAX_BUNDLE_FILES:
        raise ExportError("bundle has too many payload files")
    inventory = [
        {"path": name, "bytes": len(value), "sha256": digest(value)}
        for name, value in sorted(payloads.items())
    ]
    manifest = encode_json(
        {
            "schema_version": schema_version,
            "kind": "afk-workflow-run",
            "identity": observed["identity"],
            "files": inventory,
        }
    )
    if len(manifest) > MAX_MANIFEST_BYTES or len(manifest) + sum(
        map(len, payloads.values())
    ) > (MAX_BUNDLE_BYTES if schema_version == 1 else V2_MAX_BUNDLE_BYTES):
        raise ExportError("bundle exceeds admission limits")
    stage = Path(tempfile.mkdtemp(prefix=".afk-export-", dir=destination.parent))
    try:
        for relative, value in {**payloads, "manifest.json": manifest}.items():
            target = stage.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "schema_version": schema_version,
        "outcome": "exported",
        "identity": observed["identity"],
        "destination": str(destination),
    }


def load_source_v2(source, project, run_id, bead_id, terminal_continuation=None):
    """Load either a terminal Coordinator Run or a terminal Preflight pause."""
    preparation_path = source / "preparation.json"
    if not preparation_path.exists() and not preparation_path.is_symlink():
        observed = load_source(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
        )
        observed["run_root"] = source
        observed["preflight"] = None
        return observed

    preparation = read_json(preparation_path)
    if not isinstance(preparation, dict):
        raise ExportError("invalid Run Preparer evidence")
    preparation_status = preparation.get("preparation_status")
    if preparation_status in {
        "routed",
        "outside_help",
        "needs_clarification",
        "caller_agent",
    }:
        if terminal_continuation is not None:
            raise ExportError("terminal Acceptance Routing has no continuation")
        return load_terminal_routing(source, preparation, project, run_id, bead_id)
    if preparation_status != "paused":
        if "preflight" in preparation:
            require_directory(source / "preflight")
        observed = load_source(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
        )
        observed["run_root"] = source
        observed["preflight"] = load_optional_preflight(source, preparation)
        if observed["preflight"] is not None and observed["preflight"]["input"][
            "source"
        ] != {"kind": "bead", "id": observed["bead_id"]}:
            raise ExportError("prepared Preflight Bead identity disagrees")
        return observed

    if terminal_continuation is not None:
        raise ExportError("terminal Preflight pause has no continuation")
    # A pause is a terminal Run in its own right.  In particular, an empty
    # Coordinator directory is evidence of absence, not missing history.
    required = {
        "schema_version",
        "run",
        "bead",
        "project",
        "repository",
        "timestamps",
        "preparation_status",
        "preflight",
        "coordinator",
        "errors",
    }
    if not isinstance(preparation, dict) or set(preparation) != required:
        raise ExportError("invalid paused Run Preparer evidence")
    if preparation.get("schema_version") != 1 or preparation.get("errors") != []:
        raise ExportError("invalid paused Run Preparer evidence")
    run, project_record, bead = (
        preparation.get("run"),
        preparation.get("project"),
        preparation.get("bead"),
    )
    if (
        not exact_object(run, {"id", "artifact_root"})
        or Path(run["artifact_root"]).resolve() != source.resolve()
        or not exact_object(project_record, {"slug"})
        or not exact_object(bead, {"id"})
    ):
        raise ExportError("invalid paused Run identity")
    identity = validate_identity(project_record["slug"], run["id"])
    validate_public_identity(bead["id"], SAFE_ID, "prepared Bead")
    assert_identity(project, identity["project"], "project")
    assert_identity(run_id, identity["run_id"], "run ID")
    assert_identity(bead_id, bead["id"], "Bead ID")
    timestamps = preparation.get("timestamps")
    if not isinstance(timestamps, dict) or not isinstance(
        timestamps.get("finished_at"), str
    ):
        raise ExportError("paused Run is not terminal")
    coordinator_facts = preparation.get("coordinator")
    if (
        not isinstance(coordinator_facts, dict)
        or coordinator_facts.get("status") != "not_started"
        or coordinator_facts.get("exit_code") is not None
        or coordinator_facts.get("outcome") is not None
        or coordinator_facts.get("decision") is not None
    ):
        raise ExportError("paused Run has Coordinator evidence")
    coordinator = source / "coordinator"
    require_directory(coordinator)
    if any(coordinator.iterdir()):
        raise ExportError("paused Run has Coordinator history")
    assignment = validate_assignment(read_json(source / "assignment.json"))
    request = validate_request(read_json(source / "coordinator-request.json"))
    assignment_bead = (
        assignment.get("source", {}).get("id")
        if assignment.get("source", {}).get("kind") == "bead"
        else None
    )
    preflight = load_optional_preflight(source, preparation)
    if (
        assignment_bead != bead["id"]
        or preflight is None
        or preflight["input"]["source"] != {"kind": "bead", "id": bead["id"]}
        or preflight["output"]["decision"] != "pause"
    ):
        raise ExportError("paused Run lacks a terminal Preflight pause")
    redactions = {
        str(source.resolve()),
        assignment["workspace"],
        run["artifact_root"],
        preparation["repository"]["path"],
        preparation["repository"]["worktree"],
    }
    return {
        "identity": identity,
        "bead_id": bead["id"],
        "assignment": assignment,
        "request": request,
        "state": None,
        "output": None,
        "coordinator": coordinator,
        "redactions": {
            item
            for item in redactions
            if isinstance(item, str) and item.startswith("/")
        },
        "run_root": source,
        "preflight": preflight,
        "preparation": preparation,
    }


def load_terminal_routing(source, preparation, project, run_id, bead_id):
    """Load a sealed v2 route that intentionally stopped before Coordinator."""
    expected = {
        "schema_version",
        "run",
        "bead",
        "project",
        "repository",
        "timestamps",
        "preparation_status",
        "routing",
        "coordinator",
        "errors",
    }
    if (
        not isinstance(preparation, dict)
        or set(preparation) != expected
        or preparation.get("schema_version") != 1
        or preparation.get("errors") != []
    ):
        raise ExportError("invalid terminal Acceptance Routing preparation")
    run, project_record, bead = (
        preparation.get("run"),
        preparation.get("project"),
        preparation.get("bead"),
    )
    if (
        not exact_object(run, {"id", "artifact_root"})
        or Path(run["artifact_root"]).resolve() != source.resolve()
        or not exact_object(project_record, {"slug"})
        or not exact_object(bead, {"id"})
    ):
        raise ExportError("invalid terminal Acceptance Routing identity")
    identity = validate_identity(project_record["slug"], run["id"])
    validate_public_identity(bead["id"], SAFE_ID, "prepared Bead")
    assert_identity(project, identity["project"], "project")
    assert_identity(run_id, identity["run_id"], "run ID")
    assert_identity(bead_id, bead["id"], "Bead ID")
    if not isinstance(preparation.get("timestamps"), dict) or not isinstance(
        preparation["timestamps"].get("finished_at"), str
    ):
        raise ExportError("Acceptance Routing Run is not terminal")
    coordinator_facts = preparation.get("coordinator")
    if (
        not isinstance(coordinator_facts, dict)
        or coordinator_facts.get("status") != "not_started"
        or coordinator_facts.get("exit_code") is not None
        or coordinator_facts.get("outcome") is not None
        or coordinator_facts.get("decision") is not None
    ):
        raise ExportError("terminal Acceptance Routing has Coordinator evidence")
    require_directory(source / "coordinator")
    if any((source / "coordinator").iterdir()):
        raise ExportError("terminal Acceptance Routing has Coordinator history")
    assignment = validate_assignment(read_json(source / "assignment.json"))
    request = validate_request(read_json(source / "coordinator-request.json"))
    if assignment.get("source") != {"kind": "bead", "id": bead["id"]}:
        raise ExportError("Acceptance Routing Bead identity disagrees")
    routing = validate_prepared_routing(source, preparation["routing"])
    decision = routing["policy"]["decision"]
    expected_status = {
        "accepted": "routed",
        "outside_help": "outside_help",
        "needs_clarification": "needs_clarification",
        "caller_agent": "caller_agent",
    }.get(decision)
    if expected_status != preparation["preparation_status"]:
        raise ExportError("Acceptance Routing terminal decision disagrees")
    repository = preparation.get("repository")
    if (
        not isinstance(repository, dict)
        or not isinstance(repository.get("path"), str)
        or not isinstance(repository.get("worktree"), str)
    ):
        raise ExportError("invalid terminal Acceptance Routing repository")
    redactions = {
        str(source.resolve()),
        assignment["workspace"],
        run["artifact_root"],
        repository["path"],
        repository["worktree"],
    }
    return {
        "identity": identity,
        "bead_id": bead["id"],
        "assignment": assignment,
        "request": request,
        "state": None,
        "output": None,
        "coordinator": source / "coordinator",
        "redactions": {
            item
            for item in redactions
            if isinstance(item, str) and item.startswith("/")
        },
        "run_root": source,
        "preflight": None,
        "preparation": preparation,
        "acceptance_routing": routing,
    }


def load_optional_preflight(source, preparation):
    if "preflight" not in preparation:
        return None
    # O_NOFOLLOW on evidence files does not protect against a symlinked parent.
    # Establish that the accepted Preflight invocation itself is inside the Run
    # before reading either its required evidence or optional artifacts.
    require_directory(source / "preflight")
    preflight_input = validate_preflight_input(
        read_json(source / "preflight-input.json")
    )
    invocation_input = validate_preflight_input(
        read_json(source / "preflight" / "input.json")
    )
    if invocation_input != preflight_input:
        raise ExportError("prepared Preflight inputs disagree")
    preflight_output_raw = read_bytes(
        source / "preflight" / "output.json", MAX_JSON_BYTES
    )
    preflight_output = validate_preflight_output(
        json.loads(decode_text(preflight_output_raw)), preflight_input
    )
    facts = preparation["preflight"]
    if (
        not isinstance(facts, dict)
        or facts.get("directory") != "preflight"
        or facts.get("result") != "preflight/output.json"
        or facts.get("status") != "completed"
        or facts.get("exit_code") != 0
        or facts.get("outcome") != "completed"
        or facts.get("decision") != preflight_output["decision"]
        or preflight_output["outcome"] != "completed"
    ):
        raise ExportError("invalid prepared Preflight evidence")
    return {
        "input": preflight_input,
        "output": preflight_output,
        # Retain the exact bytes that passed the contract so the narrowly
        # privileged public transformation cannot be applied to a later file.
        "output_raw": preflight_output_raw,
    }


def load_source(
    source,
    project,
    run_id,
    bead_id,
    allow_running_continuation=False,
    terminal_continuation=None,
):
    require_directory(source)
    preparation_path = source / "preparation.json"
    if preparation_path.exists() or preparation_path.is_symlink():
        preparation = read_json(preparation_path)
        identity, prepared_bead = validate_preparation(source, preparation)
        assert_identity(project, identity["project"], "project")
        assert_identity(run_id, identity["run_id"], "run ID")
        assert_identity(bead_id, prepared_bead, "Bead ID")
        coordinator = source / "coordinator"
        root_assignment = read_json(source / "assignment.json")
        root_request = read_json(source / "coordinator-request.json")
        if "preflight" in preparation:
            preflight_input = validate_preflight_input(
                read_json(source / "preflight-input.json")
            )
            preflight_output = validate_preflight_output(
                read_json(source / "preflight" / "output.json"), preflight_input
            )
            validate_prepared_preflight(
                preparation["preflight"], preflight_input, preflight_output
            )
        acceptance_routing = None
        if "routing" in preparation:
            acceptance_routing = validate_prepared_routing(
                source, preparation["routing"]
            )
            if acceptance_routing["policy"]["decision"] != "direct":
                raise ExportError("Coordinator Run lacks an accepted direct route")
    else:
        if project is None or run_id is None:
            raise ExportUsageError(
                "direct Coordinator export requires project and run ID"
            )
        identity = validate_identity(project, run_id)
        prepared_bead = bead_id
        preparation = None
        coordinator = source
        root_assignment = None
        root_request = None
        acceptance_routing = None

    require_directory(coordinator)
    assignment = validate_assignment(read_json(coordinator / "assignment.json"))
    request = validate_request(read_json(coordinator / "input.json"))
    if root_assignment is not None and root_assignment != assignment:
        raise ExportError("Run Preparer Assignment disagrees with Coordinator")
    if root_request is not None and root_request != request:
        raise ExportError("Run Preparer request disagrees with Coordinator")
    assignment_bead = (
        assignment.get("source", {}).get("id")
        if assignment.get("source", {}).get("kind") == "bead"
        else None
    )
    if prepared_bead is not None and assignment_bead != prepared_bead:
        raise ExportError("Bead identity disagrees with Assignment")
    if prepared_bead is None:
        prepared_bead = assignment_bead
    if prepared_bead is not None:
        validate_public_identity(prepared_bead, SAFE_ID, "Bead")

    original_state = validate_checkpoint(read_json(coordinator / "state.json"))
    original_output = validate_output(read_json(coordinator / "output.json"))
    if original_state["status"] == "running" or original_output != output_from_state(
        original_state
    ):
        raise ExportError("Coordinator terminal evidence disagrees")
    if preparation is not None:
        validate_preparer_terminal(preparation, original_output)
    state, output, terminal_directory, continuations = load_continuation_lineage(
        coordinator,
        request,
        original_state,
        original_output,
        allow_running=allow_running_continuation,
        terminal_continuation=terminal_continuation,
    )
    if continuations:
        identity = {
            **identity,
            "run_id": f"{identity['run_id']}.continuation.{continuations[-1].name}",
        }
        validate_public_identity(identity["run_id"], SAFE_ID, "continuation Run ID")
    redactions = {str(source.resolve()), assignment["workspace"]}
    if preparation is not None:
        redactions.update(
            {
                preparation["run"]["artifact_root"],
                preparation["repository"]["path"],
                preparation["repository"]["worktree"],
            }
        )
    return {
        "identity": identity,
        "bead_id": prepared_bead,
        "assignment": assignment,
        "request": request,
        "state": state,
        "output": output,
        "coordinator": coordinator,
        "terminal_directory": terminal_directory,
        "continuations": continuations,
        "redactions": {
            value
            for value in redactions
            if isinstance(value, str) and value.startswith("/")
        },
        "acceptance_routing": acceptance_routing,
    }


def load_continuation_lineage(
    coordinator,
    request,
    state,
    output,
    allow_running=False,
    terminal_continuation=None,
):
    """Validate the full lineage and select one sealed terminal when requested."""
    from afk_coordinate.__main__ import (
        existing_continuations,
        require_exhausted,
        validate_continuation_link,
    )

    directories = existing_continuations(coordinator / "continuations")
    prior_output = "../../output.json"
    expected_max_responses = request["max_responses"]
    observed = []
    terminal_directory = coordinator
    selected = None
    for directory in directories:
        require_exhausted(
            coordinator, state, expected_max_responses, check_workspace=False
        )
        continuation_input = validate_continuation(read_json(directory / "input.json"))
        continuation_state = validate_checkpoint(read_json(directory / "state.json"))
        validate_continuation_link(
            state, continuation_state, continuation_input, prior_output
        )
        if continuation_state["status"] == "running":
            if (
                not allow_running
                or directory != directories[-1]
                or (directory / "output.json").exists()
            ):
                raise ExportError("newest continuation is not terminal")
            observed.append(directory)
            break
        continuation_output = validate_output(read_json(directory / "output.json"))
        if continuation_output != output_from_state(continuation_state):
            raise ExportError("continuation terminal evidence disagrees")
        state = continuation_state
        output = continuation_output
        terminal_directory = directory
        observed.append(directory)
        expected_max_responses = continuation_input["effective_max_responses"]
        prior_output = f"../{directory.name}/output.json"
        if directory.name == terminal_continuation:
            selected = (state, output, terminal_directory, list(observed))
    if terminal_continuation is not None:
        if selected is None:
            raise ExportError("selected continuation is not a sealed terminal")
        return selected
    return state, output, terminal_directory, observed


def validate_preparation(source, value):
    expected = {
        "schema_version",
        "run",
        "bead",
        "project",
        "repository",
        "timestamps",
        "preparation_status",
        "coordinator",
        "errors",
    }
    if (
        not isinstance(value, dict)
        or frozenset(value)
        not in {
            frozenset(expected),
            frozenset(expected | {"preflight"}),
            frozenset(expected | {"routing"}),
        }
        or value.get("schema_version") != 1
    ):
        raise ExportError("invalid Run Preparer evidence")
    if value["preparation_status"] != "prepared" or value["errors"] != []:
        raise ExportError("Run Preparer did not seal a prepared Run")
    run = value["run"]
    project = value["project"]
    bead = value["bead"]
    if (
        not exact_object(run, {"id", "artifact_root"})
        or Path(run["artifact_root"]).resolve() != source.resolve()
    ):
        raise ExportError("invalid Run identity")
    if not exact_object(project, {"slug"}) or not exact_object(bead, {"id"}):
        raise ExportError("invalid prepared identity")
    identity = validate_identity(project["slug"], run["id"])
    validate_public_identity(bead["id"], SAFE_ID, "prepared Bead")
    coordinator = value["coordinator"]
    if (
        not isinstance(coordinator, dict)
        or coordinator.get("directory") != "coordinator"
        or coordinator.get("result") != "coordinator/output.json"
        or coordinator.get("outcome") not in {"completed", "failed"}
        or coordinator.get("status") not in {"completed", "failed"}
        or not isinstance(coordinator.get("exit_code"), int)
        or isinstance(coordinator.get("exit_code"), bool)
    ):
        raise ExportError("invalid prepared Coordinator evidence")
    timestamps = value["timestamps"]
    if not isinstance(timestamps, dict) or not isinstance(
        timestamps.get("finished_at"), str
    ):
        raise ExportError("Run Preparer is not terminal")
    return identity, bead["id"]


def validate_prepared_preflight(prepared, preflight_input, preflight_output):
    if (
        not isinstance(prepared, dict)
        or prepared.get("directory") != "preflight"
        or prepared.get("result") != "preflight/output.json"
        or prepared.get("status") != "completed"
        or prepared.get("exit_code") != 0
        or prepared.get("outcome") != "completed"
        or prepared.get("decision") != "proceed"
        or preflight_output["outcome"] != "completed"
        or preflight_output["decision"] != "proceed"
        or preflight_output["source"] != preflight_input["source"]
    ):
        raise ExportError("invalid prepared Preflight evidence")


def validate_prepared_routing(source, prepared):
    """Validate and retain the complete, contract-bound v2 routing stage."""
    # Anchor every routing read to open descriptors. A concurrently replaced Run
    # or invocation pathname then cannot redirect output.json outside the Run.
    descriptors = []
    try:
        source_descriptor = os.open(source, DIRECTORY_FLAGS)
        descriptors.append(source_descriptor)
        planner_descriptor = os.open(
            "planner", DIRECTORY_FLAGS, dir_fd=source_descriptor
        )
        descriptors.append(planner_descriptor)
        policy_descriptor = os.open("policy", DIRECTORY_FLAGS, dir_fd=source_descriptor)
        descriptors.append(policy_descriptor)

        planner_input = validate_plan_input(
            read_json_at(source_descriptor, "planner-input.json")
        )
        planner_raw = read_bytes_at(planner_descriptor, "output.json", MAX_JSON_BYTES)
        planner_output = validate_planner_output(
            planner_input, json.loads(decode_text(planner_raw))
        )
        evidence_name = "routing" if planner_output["plan"] is None else "plan"
        evidence = planner_output[evidence_name]
        policy_input = read_json_at(source_descriptor, "policy-input.json")
        policy_raw = read_bytes_at(policy_descriptor, "output.json", MAX_JSON_BYTES)
        policy_output = validate_policy_output(
            planner_input, policy_input, json.loads(decode_text(policy_raw))
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise ExportError("invalid prepared Acceptance Routing evidence") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if (
        not isinstance(prepared, dict)
        or set(prepared) != {"planner", "policy"}
        or policy_input
        != {
            "schema_version": 2,
            "planner_input": planner_input,
            evidence_name: evidence,
        }
    ):
        raise ExportError("invalid prepared Acceptance Routing evidence")
    expected_exit = 0 if policy_output["decision"] in {"direct", "accepted"} else 1
    expected = (
        (prepared["planner"], "planner", 0, planner_output["outcome"], None),
        (
            prepared["policy"],
            "policy",
            expected_exit,
            policy_output["outcome"],
            policy_output["decision"],
        ),
    )
    for facts, directory, exit_code, outcome, decision in expected:
        if (
            not isinstance(facts, dict)
            or facts.get("directory") != directory
            or facts.get("result") != f"{directory}/output.json"
            or facts.get("status") != "completed"
            or facts.get("exit_code") != exit_code
            or facts.get("outcome") != outcome
            or (decision is not None and facts.get("decision") != decision)
        ):
            raise ExportError("invalid prepared Acceptance Routing evidence")
    return {
        "planner": planner_output,
        "policy": policy_output,
        "planner_raw": planner_raw,
        "policy_raw": policy_raw,
    }


def validate_preparer_terminal(preparation, output):
    coordinator = preparation["coordinator"]
    if coordinator["outcome"] != output["outcome"]:
        raise ExportError("Run Preparer outcome disagrees with Coordinator")
    decision = output.get("decision") if output["outcome"] == "completed" else None
    if coordinator["decision"] != decision:
        raise ExportError("Run Preparer decision disagrees with Coordinator")
    expected_status = (
        "completed"
        if output["outcome"] == "completed" and coordinator["exit_code"] == 0
        else "failed"
    )
    if coordinator["status"] != expected_status:
        raise ExportError("Run Preparer status disagrees with Coordinator")


def normalize_run_v2(observed):
    """Create the v2 semantic record and its sanitized public artifacts."""
    if observed["state"] is None:
        routing = observed.get("acceptance_routing")
        record = {
            "schema_version": 2,
            "identity": observed["identity"],
            "bead": {"id": observed["bead_id"]},
            "objective": bounded_text(
                observed["assignment"]["objective"], observed["redactions"]
            ),
            "response_limit": observed["request"]["max_responses"],
            "status": (
                observed["preparation"]["preparation_status"] if routing else "paused"
            ),
            "terminal": (
                {
                    "stage": "acceptance_routing",
                    "decision": routing["policy"]["decision"],
                }
                if routing
                else {"stage": "preflight", "decision": "pause"}
            ),
            "history": [],
            "evidence": [],
        }
        if routing:
            record["acceptance_routing"] = normalize_acceptance_routing(
                routing, observed["redactions"]
            )
        else:
            preflight = sanitize_json_value(
                observed["preflight"]["output"], observed["redactions"]
            )[0]
            record["preflight"] = {
                "outcome": preflight["outcome"],
                "decision": preflight["decision"],
                "requests": preflight["requests"],
            }
    else:
        record, _ = normalize_run(observed, include_evidence=False)
        record["schema_version"] = 2
        if observed.get("preflight"):
            public, _ = sanitize_json_value(
                observed["preflight"]["output"], observed["redactions"]
            )
            record["preflight"] = {
                "outcome": public["outcome"],
                "decision": public["decision"],
                "requests": public["requests"],
            }
        if observed.get("acceptance_routing"):
            record["acceptance_routing"] = normalize_acceptance_routing(
                observed["acceptance_routing"], observed["redactions"]
            )

    descriptors, payloads = public_artifacts(observed)
    record["artifacts"] = descriptors
    return record, payloads


def normalize_acceptance_routing(value, redactions=frozenset()):
    """Reduce validated routing envelopes to bounded public semantic facts."""
    planner, policy = value["planner"], value["policy"]
    routing = planner["routing"]
    result = {
        "planner": {
            "outcome": planner["outcome"],
            "route_kind": "direct" if planner["plan"] is None else "decomposed",
            "routing_status": routing["status"],
        },
        "policy": {
            "outcome": policy["outcome"],
            "decision": policy["decision"],
        },
    }
    if policy["error_category"] is not None:
        # This contract enum is the exact trusted outside-help/clarification
        # reason.  Do not replace it with prose inferred from Planner content.
        result["reason"] = policy["error_category"]
    if planner["plan"] is None:
        result["route"] = {
            "kind": "direct",
            "routes": [normalize_route(item) for item in routing["routes"]],
        }
    else:
        result["route"] = {
            "kind": "decomposed",
            "children": [normalize_child(item) for item in planner["plan"]["children"]],
        }
    result["artifacts"] = [
        {"type": "planner", "source": "planner/output.json"},
        {"type": "policy", "source": "policy/output.json"},
    ]
    return sanitize_json_value(result, redactions)[0]


def normalize_route(route):
    fields = (
        "criterion",
        "target",
        "project",
        "owner",
        "phase",
        "executor",
        "evidence_route",
        "outside_help_reason",
    )
    return {field: route[field] for field in fields if field in route}


def normalize_child(child):
    fields = (
        "local_id",
        "project",
        "owner",
        "phase",
        "executor",
        "evidence_route",
        "outside_help_reason",
        "depends_on",
        "readiness",
    )
    return {field: child[field] for field in fields if field in child}


def public_artifacts(observed):
    candidates = artifact_candidates(observed)
    descriptors = []
    payloads = {}
    # Structured records and human-readable logs are admitted before event
    # streams.  Stable sorting makes the policy independent of filesystem order.
    candidates.sort(key=lambda item: (item["priority"], item["source"]))
    budget = V2_MAX_BUNDLE_BYTES - MAX_MANIFEST_BYTES - MAX_INCLUDED_BYTES
    used = 0
    for candidate in candidates:
        descriptor, data = derive_public_artifact(candidate, observed["redactions"])
        if data is not None and (
            used + len(data) > budget or len(payloads) >= MAX_BUNDLE_FILES - 1
        ):
            descriptor.update(
                state="oversized",
                public_bytes=0,
                public_sha256=None,
                sanitization_status="not_applicable",
                unavailable_reason="bundle_limit",
            )
            descriptor.pop("path", None)
            data = None
        if data is not None:
            if descriptor["path"] in payloads:
                raise ExportError("public artifact destinations collide")
            used += len(data)
            payloads[descriptor["path"]] = data
        descriptors.append(descriptor)
    return descriptors, payloads


def artifact_candidates(observed):
    root = observed["run_root"]
    result = []
    seen = set()

    def add(
        relative,
        scope,
        kind,
        media_type,
        priority,
        unsafe_path=False,
        declaration=None,
        validated_preflight_classifier_key=None,
        validated_preflight_output_raw=None,
        validated_raw=None,
    ):
        if not unsafe_path and not safe_relative(relative):
            return
        # A source filename is not a semantic identity: separate artifact
        # declarations may intentionally name the same file, and a synthetic
        # unsafe-path descriptor may collide with a real basename.  Deduplicate
        # only truly identical candidates so every declaration remains visible.
        identity = (
            relative,
            scope,
            kind,
            media_type,
            priority,
            unsafe_path,
            declaration,
        )
        if identity in seen:
            return
        seen.add(identity)
        result.append(
            {
                "root": root,
                "source": relative,
                "scope": scope,
                "kind": kind,
                "media_type": media_type,
                "priority": priority,
                "unsafe_path": unsafe_path,
                "declaration": declaration,
                "validated_preflight_classifier_key": (
                    validated_preflight_classifier_key
                ),
                "validated_preflight_output_raw": validated_preflight_output_raw,
                "validated_raw": validated_raw,
            }
        )

    # Only accepted Run-relative payload names are considered.  Private paths and
    # command credentials inside structured records are sanitized field by field.
    if observed["coordinator"].resolve() != root.resolve():
        for name in (
            "bead.json",
            "assignment.json",
            "coordinator-request.json",
            "preparation.json",
        ):
            add(name, "run", "json", "application/json", 0)
    if observed.get("preflight"):
        add("preflight-input.json", "preflight", "json", "application/json", 0)
        add("preflight/input.json", "preflight", "json", "application/json", 0)
        add(
            "preflight/output.json",
            "preflight",
            "json",
            "application/json",
            0,
            validated_preflight_classifier_key=observed["preflight"]["output"][
                "classifier"
            ].get("key"),
            validated_preflight_output_raw=observed["preflight"]["output_raw"],
        )
        add("preflight/stderr.log", "preflight", "log", "text/plain; charset=utf-8", 1)
        add("preflight/events.jsonl", "preflight", "events", "application/x-ndjson", 2)
    if observed.get("acceptance_routing"):
        # Planner event streams can contain model prompts and policy input repeats
        # the private catalog.  Publish only the two validated typed envelopes.
        add(
            "planner/output.json",
            "acceptance_routing",
            "planner",
            "application/json",
            0,
            validated_raw=observed["acceptance_routing"]["planner_raw"],
        )
        add(
            "policy/output.json",
            "acceptance_routing",
            "policy",
            "application/json",
            0,
            validated_raw=observed["acceptance_routing"]["policy_raw"],
        )
    if observed["state"] is not None:
        coordinator_prefix = (
            ""
            if observed["coordinator"].resolve() == root.resolve()
            else "coordinator/"
        )
        for name in ("assignment.json", "input.json", "state.json", "output.json"):
            add(
                f"{coordinator_prefix}{name}",
                "coordinator",
                "json",
                "application/json",
                0,
            )
        for continuation in observed.get("continuations", []):
            relative = continuation.relative_to(observed["coordinator"]).as_posix()
            for name in ("input.json", "state.json", "output.json"):
                add(
                    f"{coordinator_prefix}{relative}/{name}",
                    f"continuation:{continuation.name}",
                    "json",
                    "application/json",
                    0,
                )
        for entry in observed["state"]["history"]:
            if entry["outcome"] == "abandoned":
                continue
            base = f"{coordinator_prefix}{entry['directory']}"
            scope = f"component:{entry['sequence']}:{entry['component']}"
            add(f"{base}/input.json", scope, "json", "application/json", 0)
            add(f"{base}/output.json", scope, "json", "application/json", 0)
            output = read_json(root / base / "output.json")
            for kind, filename in sorted(output.get("artifacts", {}).items()):
                if kind not in ARTIFACTS[entry["component"]] or not isinstance(
                    filename, str
                ):
                    continue
                artifact_kind = (
                    "events"
                    if kind == "events"
                    else ("diff" if kind == "diff" else "log")
                )
                media = (
                    "application/x-ndjson"
                    if kind == "events"
                    else (
                        "text/x-diff; charset=utf-8"
                        if kind == "diff"
                        else "text/plain; charset=utf-8"
                    )
                )
                # Component contracts allow arbitrary strings here, but the
                # publication allowlist is deliberately basename-only.  Keep
                # rejected declarations visible under a synthetic safe source
                # identity without ever resolving the declared path.
                safe_name = (
                    Path(filename).name == filename
                    and safe_relative(filename)
                    and safe_public_artifact_name(filename, observed["redactions"])
                )
                add(
                    f"{base}/{filename}" if safe_name else f"{base}/declared-{kind}",
                    scope,
                    artifact_kind,
                    media,
                    2 if kind == "events" else 1,
                    unsafe_path=not safe_name,
                    declaration=kind,
                )

    # Colliding published candidates need distinct bundle paths even though
    # they correctly retain the same Run-relative source identity.
    source_counts = {}
    for candidate in result:
        if not candidate["unsafe_path"]:
            source_counts[candidate["source"]] = (
                source_counts.get(candidate["source"], 0) + 1
            )
    for candidate in result:
        if not candidate["unsafe_path"] and source_counts[candidate["source"]] > 1:
            suffix = candidate.get("declaration") or candidate["kind"]
            candidate["destination"] = f"artifacts/{candidate['source']}.{suffix}"

    # Source disambiguation can itself produce another candidate's natural
    # destination (for example output.json.json versus output.json plus its
    # semantic ``.json`` suffix).  Reserve destinations globally in stable
    # candidate order so payload assembly can never silently overwrite bytes.
    destinations = set()
    for candidate in result:
        if candidate["unsafe_path"]:
            continue
        desired = candidate.get("destination", f"artifacts/{candidate['source']}")
        destination = desired
        duplicate = 2
        while destination in destinations:
            destination = f"{desired}.duplicate-{duplicate}"
            duplicate += 1
        candidate["destination"] = destination
        destinations.add(destination)
    return result


def derive_public_artifact(candidate, redactions):
    source = candidate["source"]
    base = {
        "source": {"path": source},
        "scope": candidate["scope"],
        "kind": candidate["kind"],
        "media_type": candidate["media_type"],
    }
    if candidate.get("unsafe_path"):
        return nondownloadable_descriptor(base, "unsafe", "unsafe_path"), None
    path = candidate["root"] / source
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return nondownloadable_descriptor(base, "unavailable", "missing"), None
    except (OSError, ValueError):
        return nondownloadable_descriptor(base, "unavailable", "unavailable"), None
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISREG(facts.st_mode):
        return nondownloadable_descriptor(base, "unsafe", "unsafe_file"), None
    if facts.st_size == 0:
        return nondownloadable_descriptor(base, "empty", "empty"), None
    if facts.st_size > V2_MAX_ARTIFACT_BYTES:
        return nondownloadable_descriptor(base, "oversized", "artifact_limit"), None
    try:
        raw = read_bytes(path, V2_MAX_ARTIFACT_BYTES, expected_facts=facts)
    except (ExportError, OSError):
        # Optional publication evidence may disappear, become unreadable, or
        # be replaced after lstat.  Seal that observation without rejecting a
        # valid terminal Run and without publishing bytes from the race.
        return nondownloadable_descriptor(base, "unavailable", "unavailable"), None
    try:
        validated_preflight_output_raw = candidate.get("validated_preflight_output_raw")
        if (
            validated_preflight_output_raw is not None
            and raw != validated_preflight_output_raw
        ):
            raise ExportError("validated Preflight output changed")
        if (
            candidate.get("validated_raw") is not None
            and raw != candidate["validated_raw"]
        ):
            raise ExportError("validated Acceptance Routing output changed")
        text = decode_text(raw)
        if candidate["kind"] in {"json", "planner", "policy"}:
            value = json.loads(text)
            changed = sanitize_validated_preflight_classifier_key(
                value, candidate.get("validated_preflight_classifier_key")
            )
            value, generally_changed = sanitize_json_value(value, redactions)
            changed = changed or generally_changed
            public = encode_json(value)
        elif candidate["kind"] == "events":
            lines = []
            changed = False
            for line in text.splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                value, item_changed = sanitize_json_value(value, redactions)
                changed = changed or item_changed
                lines.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
            if not lines:
                return nondownloadable_descriptor(base, "empty", "empty"), None
            public = ("\n".join(lines) + "\n").encode()
        else:
            sanitized = sanitize_public_artifact_text(text, redactions)
            changed = sanitized != text
            public = sanitized.encode()
    except (
        ExportError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return nondownloadable_descriptor(base, "unsafe", "unsafe_or_invalid"), None
    if len(public) > V2_MAX_ARTIFACT_BYTES:
        return nondownloadable_descriptor(base, "oversized", "artifact_limit"), None
    destination = candidate.get("destination", "artifacts/" + source)
    descriptor = {
        **base,
        "state": "downloadable",
        "public_bytes": len(public),
        "public_sha256": digest(public),
        "sanitization_status": "sanitized" if changed or public != raw else "unchanged",
        "unavailable_reason": None,
        "path": destination,
    }
    return descriptor, public


def sanitize_validated_preflight_classifier_key(value, expected_key):
    """Replace only a classifier key accepted by the Preflight output contract."""
    if expected_key is None:
        return False
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("classifier"), dict)
        or value["classifier"].get("key") != expected_key
    ):
        # The source changed after validation or is not the validated record.
        raise ExportError("validated Preflight classifier key disagrees")
    value["classifier"]["key"] = PUBLIC_PREFLIGHT_CLASSIFIER_KEY
    return True


def nondownloadable_descriptor(base, state, reason):
    return {
        **base,
        "state": state,
        "public_bytes": 0,
        "public_sha256": None,
        "sanitization_status": "not_applicable",
        "unavailable_reason": reason,
    }


def sanitize_json_value(value, redactions):
    if isinstance(value, str):
        option = CREDENTIAL_OPTION_VALUE.fullmatch(value)
        if option:
            return f"{option.group(1)}={REDACTED_SECRET}", True
        public = sanitize_public_artifact_text(value, redactions)
        return public, public != value
    if isinstance(value, list):
        result, changed = [], False
        redact_next = False
        for item in value:
            if redact_next and isinstance(item, str):
                public, item_changed = REDACTED_SECRET, item != REDACTED_SECRET
            else:
                public, item_changed = sanitize_json_value(item, redactions)
            result.append(public)
            changed = changed or item_changed
            redact_next = isinstance(item, str) and bool(
                CREDENTIAL_OPTION.fullmatch(item)
            )
        return result, changed
    if isinstance(value, dict):
        result, changed = {}, False
        for key in sorted(value):
            if not isinstance(key, str):
                raise ExportError("JSON object key is not text")
            public_key = sanitize_public_text(key, redactions)
            public, item_changed = sanitize_json_value(value[key], redactions)
            if public_key in result:
                raise ExportError("sanitized JSON keys collide")
            result[public_key] = public
            changed = changed or item_changed or public_key != key
        return result, changed
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportError("non-finite JSON number")
        return value, False
    raise ExportError("unsupported JSON value")


def normalize_run(observed, include_evidence=True):
    state = observed["state"]
    directories = {entry["directory"]: entry["sequence"] for entry in state["history"]}
    history = []
    evidence = []
    payloads = {}
    for entry in state["history"]:
        component = entry["component"]
        normalized = {"outcome": entry["outcome"]}
        invocation_evidence = []
        if entry["outcome"] != "abandoned":
            directory = observed["coordinator"] / entry["directory"]
            require_directory(directory)
            output = read_json(directory / "output.json")
            if validate_component_output(component, output) != entry["outcome"]:
                raise ExportError("Component output disagrees with Coordinator history")
            normalized = normalize_component_output(
                component, output, observed["redactions"]
            )
            if include_evidence:
                invocation_evidence, files = normalize_evidence(
                    entry, directory, output, observed["redactions"]
                )
                evidence.extend(invocation_evidence)
                payloads.update(files)
        inputs = sorted(
            {
                directories[source]
                for source in entry["input_from"].values()
                if source in directories
            }
        )
        history.append(
            {
                "sequence": entry["sequence"],
                "component": component,
                "outcome": entry["outcome"],
                "inputs": inputs,
                "output": normalized,
                "evidence": [item["kind"] for item in invocation_evidence],
            }
        )
    record = {
        "schema_version": 1,
        "identity": observed["identity"],
        **({"bead": {"id": observed["bead_id"]}} if observed["bead_id"] else {}),
        "objective": bounded_text(
            observed["assignment"]["objective"], observed["redactions"]
        ),
        "response_limit": observed["request"]["max_responses"],
        "status": state["status"],
        "terminal": state["terminal"],
        "history": history,
        "evidence": evidence,
    }
    encoded = encode_json(record)
    for redaction in observed["redactions"]:
        if redaction.encode() in encoded:
            raise ExportError("normalized Run contains a host path")
    return record, payloads


def normalize_component_output(component, value, redactions):
    result = {"outcome": value["outcome"]}
    for field in ("started_at", "finished_at"):
        if field in value:
            result[field] = bounded_text(value[field], redactions)
    if "duration_seconds" in value:
        duration = value["duration_seconds"]
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration < 0
        ):
            raise ExportError("invalid component duration")
        result["duration_seconds"] = duration
    if "process" in value:
        process = value["process"]
        if not isinstance(process, dict):
            raise ExportError("invalid process facts")
        result["process"] = {
            "exit_code": integer_or_none(process.get("exit_code")),
            "signal": integer_or_none(process.get("signal")),
            **({"error_category": "execution_error"} if process.get("error") else {}),
        }
    if "agent" in value:
        agent = value["agent"]
        if not isinstance(agent, dict) or not isinstance(agent.get("status"), str):
            raise ExportError("invalid agent facts")
        result["agent"] = {
            "status": bounded_text(agent["status"], redactions),
            **({"error_category": "protocol_error"} if agent.get("error") else {}),
        }
    if "repository" in value:
        result["repository"] = normalize_repository(value["repository"], redactions)
    if value["outcome"] == COMPONENT_TOPOLOGY[component]["success"]:
        result["details"] = component_details(component, value, redactions)
    else:
        result["details"] = {"kind": component}
    return result


def component_details(component, value, redactions):
    if component == "review":
        review = value.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
            raise ExportError("invalid Review details")
        return {
            "kind": "review",
            "summary": bounded_text(review.get("summary"), redactions),
            "findings": [
                normalize_finding(item, redactions) for item in review["findings"]
            ],
        }
    if component == "assessment":
        assessment = value.get("assessment")
        if not isinstance(assessment, dict) or not isinstance(
            assessment.get("decisions"), list
        ):
            raise ExportError("invalid Assessment details")
        if not all(isinstance(item, dict) for item in assessment["decisions"]):
            raise ExportError("invalid Assessment decision")
        return {
            "kind": "assessment",
            "summary": bounded_text(assessment.get("summary"), redactions),
            "decisions": [
                {
                    "finding_index": nonnegative_integer(item.get("finding_index")),
                    "worth_addressing": require_boolean(item.get("worth_addressing")),
                    "rationale": bounded_text(item.get("rationale"), redactions),
                }
                for item in assessment["decisions"]
            ],
        }
    if component == "response":
        response = value.get("response")
        if not isinstance(response, dict) or not isinstance(
            response.get("finding_responses"), list
        ):
            raise ExportError("invalid Response details")
        if not all(isinstance(item, dict) for item in response["finding_responses"]):
            raise ExportError("invalid Response finding response")
        return {
            "kind": "response",
            "summary": bounded_text(response.get("summary"), redactions),
            "finding_responses": [
                {
                    "finding_index": nonnegative_integer(item.get("finding_index")),
                    "response": bounded_text(item.get("response"), redactions),
                }
                for item in response["finding_responses"]
            ],
        }
    if component == "iteration":
        policy = value.get("policy")
        if not isinstance(policy, dict) or policy.get("decision") not in {
            "continue",
            "stop",
            "exhausted",
        }:
            raise ExportError("invalid Iteration details")
        details = {"kind": "iteration", "decision": policy["decision"]}
        for field in ("completed_responses", "max_responses", "actionable_findings"):
            details[field] = nonnegative_integer(policy.get(field))
        if policy["decision"] == "continue":
            details["next_response_number"] = positive_integer(
                policy.get("next_response_number")
            )
        elif "next_response_number" in policy:
            raise ExportError("terminal Iteration has a response number")
        details["reason"] = bounded_text(policy.get("reason"), redactions)
        return details
    return {"kind": component}


def normalize_finding(value, redactions):
    if not isinstance(value, dict) or not isinstance(value.get("locations"), list):
        raise ExportError("invalid Review finding")
    locations = []
    for location in value["locations"]:
        if not isinstance(location, dict) or not safe_relative(location.get("path")):
            raise ExportError("invalid Review location")
        locations.append(
            {"path": location["path"], "line": positive_integer(location.get("line"))}
        )
    return {
        "severity": bounded_text(value.get("severity"), redactions),
        "title": bounded_text(value.get("title"), redactions),
        "details": bounded_text(value.get("details"), redactions),
        "locations": locations,
    }


def normalize_repository(value, redactions):
    if not isinstance(value, dict):
        raise ExportError("invalid repository facts")
    result = {}
    for field in ("before", "after"):
        state = value.get(field)
        if state is None:
            result[field] = None
            continue
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("dirty"), bool)
            or not isinstance(state.get("status"), list)
        ):
            raise ExportError("invalid repository state")
        result[field] = {
            "head": optional_text(state.get("head"), redactions),
            "branch": optional_text(state.get("branch"), redactions),
            "dirty": state["dirty"],
            "status": [bounded_text(item, redactions) for item in state["status"]],
        }
    for field in ("commits_between_heads",):
        if field in value:
            commits = value[field]
            result[field] = (
                None
                if commits is None
                else [bounded_text(item, redactions) for item in commits]
            )
    for field in ("head_changed", "unchanged", "descends_from_before"):
        if field in value:
            result[field] = require_boolean(value[field])
    if value.get("observation_error"):
        result["observation_error"] = True
    return result


def normalize_evidence(entry, directory, output, redactions):
    artifacts = output.get("artifacts", {})
    if not isinstance(artifacts, dict) or not set(artifacts).issubset(
        ARTIFACTS[entry["component"]]
    ):
        raise ExportError("component artifacts are invalid")
    descriptors = []
    payloads = {}
    for kind, filename in artifacts.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ExportError("artifact path is invalid")
        path = directory / filename
        limit = MAX_EVENTS_BYTES if kind == "events" else MAX_INCLUDED_BYTES
        data = read_bytes(path, limit)
        text = decode_text(data)
        if kind == "events":
            descriptors.append(
                evidence_descriptor(
                    entry, kind, data, text, "omitted", event_counts(text)
                )
            )
        elif data:
            text = sanitize_public_text(text, redactions)
            data = text.encode()
            relative = f"evidence/{entry['sequence']:02d}-{entry['component']}/{INCLUDED_NAMES[kind]}"
            descriptors.append(
                evidence_descriptor(entry, kind, data, text, "included", path=relative)
            )
            payloads[relative] = data
    return descriptors, payloads


def sanitize_public_text(text, redactions):
    text = redact_public_paths(text, redactions)
    if any(pattern.search(text) for pattern in SENSITIVE_TEXT):
        raise ExportError("included Evidence contains sensitive text")
    return text


def sanitize_public_artifact_text(text, redactions):
    """Derive public v2 text by redacting paths and replaceable credentials."""
    text = redact_public_paths(text, redactions)
    if PRIVATE_KEY_TEXT.search(text):
        raise ExportError("artifact contains unsafe private key material")
    for pattern in REDACTABLE_CREDENTIAL_TEXT:
        text = pattern.sub(REDACTED_SECRET, text)
    return text


def redact_public_paths(text, redactions):
    for prefix in sorted(redactions, key=len, reverse=True):
        text = text.replace(prefix, "[redacted-path]")
    text = HOST_PATH.sub("[redacted-path]", text)
    return text


def evidence_descriptor(entry, kind, data, text, inclusion, counts=None, path=None):
    return {
        "sequence": entry["sequence"],
        "component": entry["component"],
        "kind": kind,
        "bytes": len(data),
        "lines": len(text.split("\n")) if data else 0,
        "sha256": digest(data),
        "inclusion": inclusion,
        **({"event_counts": counts} if counts is not None else {}),
        **({"path": path} if path is not None else {}),
    }


def event_counts(text):
    counts = {name: 0 for name in sorted(EVENT_TYPES)}
    counts["unknown"] = 0
    for line in text.splitlines():
        try:
            event_type = json.loads(line).get("type")
        except (AttributeError, json.JSONDecodeError):
            event_type = None
        counts[event_type if event_type in EVENT_TYPES else "unknown"] += 1
    return counts


def output_from_state(state):
    if state["status"] == "completed":
        return {
            "schema_version": 1,
            "outcome": "completed",
            **state["terminal"],
            "history": state["history"],
        }
    if state["status"] == "failed":
        return {
            "schema_version": 1,
            "outcome": "failed",
            **state["terminal"],
            "history": state["history"],
        }
    raise ExportError("Coordinator is not terminal")


def validate_identity(project, run_id):
    validate_public_identity(project, SAFE_PROJECT, "Project")
    validate_public_identity(run_id, SAFE_ID, "Run")
    return {"project": project, "run_id": run_id}


def validate_public_identity(value, pattern, name):
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or any(sensitive.search(value) for sensitive in SENSITIVE_TEXT)
    ):
        raise ExportError(f"invalid {name} identity")


def assert_identity(assertion, observed, name):
    if assertion is not None and assertion != observed:
        raise ExportError(f"{name} assertion disagrees with sealed evidence")


def exact_object(value, fields):
    return isinstance(value, dict) and set(value) == fields


def require_directory(path):
    facts = path.lstat()
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
        raise ExportError("Run path must be a real directory")


def read_json(path):
    return json.loads(decode_text(read_bytes(path, MAX_JSON_BYTES)))


def read_json_at(directory_descriptor, name):
    return json.loads(
        decode_text(read_bytes_at(directory_descriptor, name, MAX_JSON_BYTES))
    )


def read_bytes(path, limit, expected_facts=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    return read_open_descriptor(descriptor, limit, expected_facts)


def read_bytes_at(directory_descriptor, name, limit, expected_facts=None):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
    return read_open_descriptor(descriptor, limit, expected_facts)


def read_open_descriptor(descriptor, limit, expected_facts=None):
    try:
        facts = os.fstat(descriptor)
        if expected_facts is not None and (
            facts.st_dev != expected_facts.st_dev
            or facts.st_ino != expected_facts.st_ino
        ):
            raise ExportError("artifact was replaced before being read")
        if not stat.S_ISREG(facts.st_mode) or facts.st_size > limit:
            raise ExportError("artifact is not a bounded regular file")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != facts.st_size:
            raise ExportError("artifact changed while being read")
        return bytes(data)
    finally:
        os.close(descriptor)


def decode_text(value):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError("artifact is not UTF-8") from error


def bounded_text(value, redactions):
    if not isinstance(value, str) or "\0" in value or len(value.encode()) > 64 * 1024:
        raise ExportError("public text is invalid")
    return sanitize_public_text(value, redactions)


def optional_text(value, redactions):
    return None if value is None else bounded_text(value, redactions)


def integer_or_none(value):
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise ExportError("integer fact is invalid")
    return value


def nonnegative_integer(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExportError("nonnegative integer is invalid")
    return value


def positive_integer(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExportError("positive integer is invalid")
    return value


def require_boolean(value):
    if not isinstance(value, bool):
        raise ExportError("boolean fact is invalid")
    return value


def safe_public_artifact_name(value, redactions):
    """Return whether a private declaration is safe to expose as metadata."""
    try:
        return (
            len(value.encode()) <= V2_MAX_ARTIFACT_NAME_BYTES
            and sanitize_public_text(value, redactions) == value
        )
    except (ExportError, UnicodeError):
        return False


def safe_relative(value):
    return (
        isinstance(value, str)
        and value
        and "\0" not in value
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode_json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
