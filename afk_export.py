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
from afk_attempt.transcript import build_attempt_transcript, encode_attempt_transcript

SUPPORTED_THINKING = {"off", "minimal", "low", "medium", "high", "xhigh"}

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
from afk_related_work import SNAPSHOT_NAME, validate_reference, validate_snapshot
from afk_review.contract import validate_audit

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
    "compaction_start",
    "compaction_end",
    "auto_retry_start",
    "auto_retry_end",
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
# A component may contain horizontal whitespace, but not at either edge.  The
# edge rule is important: without it, ``/tmp/a and compare /tmp/b`` is parsed
# as one path whose second component is "a and compare ".
POSIX_PATH_COMPONENT = r"[^\s/'\"`](?:[^\r\n/'\"`]*?[^\s/'\"`])?"
WINDOWS_PATH_COMPONENT = r"[^\s:\\/'\"`](?:[^\r\n:\\/'\"`]*?[^\s:\\/'\"`])?"
HOST_PATH = re.compile(
    r"(?:"
    # A spaced final filename is unambiguous when its last word has a file
    # extension.  Stop at that extension rather than consuming later prose.
    r"(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*"
    r"[^\s/'\"`]+(?:[ \t]+[^\s/'\"`]+)+?\.[A-Za-z0-9]{1,16}"
    r"(?![A-Za-z0-9._-])"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+(?:[ \t]+[^\s\\/'\"`]+)+?"
    r"\.[A-Za-z0-9]{1,16}(?![A-Za-z0-9._-]))"
    # If a string consists solely of a path, its final component can safely
    # contain spaces even without an extension.
    r"|\A/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*" + POSIX_PATH_COMPONENT + r"\Z"
    r"|\A[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*"
    + WINDOWS_PATH_COMPONENT
    + r"\Z"
    # General paths retain prose by allowing spaces only in completed,
    # separator-terminated components and using a whitespace-free final one.
    r"|(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*[^\s/'\"`]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+)"
    r"|(?<![\\])\\\\" + WINDOWS_PATH_COMPONENT + r"[\\/]"
    r"(?:" + WINDOWS_PATH_COMPONENT + r"[\\/])*[^\s\\/'\"`]+"
    r")"
)
# Embedded paths whose final component may contain spaces but has no
# recognizable prose boundary cannot be safely separated from following text.
# Reject them rather than publishing a suffix after HOST_PATH redacts only the
# first word. Paths matched above *with* whitespace are unambiguous (an
# extension, an intermediate spaced component, or a whole-string path).
PATH_PROSE_BOUNDARY = (
    r"(?:and|or|but|before|after|then|while|when|where|which|that|to|for|from|"
    r"with|without|is|was|must|should|can)\b"
)
AMBIGUOUS_SPACED_FINAL_PATH = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9./])/(?!/)(?:" + POSIX_PATH_COMPONENT + r"/)*"
    r"[^\s/'\"`]+[ \t]+(?!" + PATH_PROSE_BOUNDARY + r")[^\r\n/'\"`]+"
    r"|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/](?:"
    + WINDOWS_PATH_COMPONENT
    + r"[\\/])*[^\s\\/'\"`]+[ \t]+(?!"
    + PATH_PROSE_BOUNDARY
    + r")[^\r\n\\/'\"`]+)"
    r"|(?<![\\])\\\\" + WINDOWS_PATH_COMPONENT + r"[\\/]"
    r"(?:" + WINDOWS_PATH_COMPONENT + r"[\\/])*"
    r"[^\s\\/'\"`]+[ \t]+(?!" + PATH_PROSE_BOUNDARY + r")[^\r\n\\/'\"`]+"
    r")",
    re.IGNORECASE,
)
ABSOLUTE_HOST_REFERENCE = re.compile(
    r"(?:\bfile://|(?<![A-Za-z0-9./])/(?!/)|(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\))",
    re.IGNORECASE,
)
SENSITIVE_JSON_KEY = re.compile(
    r"(?i)^(?:(?:[a-z0-9]+[_-])*(?:password|passwd|passphrase|token|secret)"
    r"(?:[_-]?key)?|(?:[a-z0-9]+[_-])*(?:access|api|private|ssh|encryption|signing)"
    r"[_-]?key|auth(?:orization)?|credentials?|cookie|session[_-]?id|"
    r"aws[_-]?secret[_-]?access[_-]?key)$"
)
JSON_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
CREDENTIAL_OPTION = re.compile(
    r"(?i)^--(?:access[-_]?token|api[-_]?key|client[-_]?secret|password|secret|token)$"
)
CREDENTIAL_OPTION_VALUE = re.compile(
    r"(?i)^(--(?:access[-_]?token|api[-_]?key|client[-_]?secret|"
    r"password|secret|token))=(.*)$"
)
REDACTED_SECRET = "[redacted-secret]"
PUBLIC_PREFLIGHT_CLASSIFIER_KEY = "[sanitized-preflight-classifier-key]"
INFERENCE_JSON_KINDS = {
    "inference_receipt",
    "inference_receipt_view",
    "inference_invocation",
    "inference_prompt",
    "inference_contract",
    "inference_response",
    "inference_task_data",
    "inference_response_view",
    "inference_terminal_response_view",
}
SHA256_TEXT = re.compile(r"[0-9a-f]{64}\Z")


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
    schema_version=3,
    terminal_continuation=None,
):
    if schema_version not in {1, 2, 3}:
        raise ExportUsageError("unsupported Publication Bundle schema")
    source_input = Path(source_path).absolute()
    destination_input = Path(destination_path).absolute()
    source_facts = require_directory(source_input)
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
    try:
        # Keep the validated Run open throughout loading. Routing evidence can
        # then be read from this directory rather than a pathname redirected by
        # a concurrent Run or ancestor replacement.
        source_descriptor = os.open(source, DIRECTORY_FLAGS)
    except OSError as error:
        raise ExportError("Run source is unavailable") from error
    opened_source_facts = os.fstat(source_descriptor)
    if (opened_source_facts.st_dev, opened_source_facts.st_ino) != (
        source_facts.st_dev,
        source_facts.st_ino,
    ):
        os.close(source_descriptor)
        raise ExportError("Run source changed during validation")
    try:
        observed = (
            load_source(
                source,
                project,
                run_id,
                bead_id,
                terminal_continuation=terminal_continuation,
                source_descriptor=source_descriptor,
            )
            if schema_version == 1
            else load_source_v2(
                source,
                project,
                run_id,
                bead_id,
                terminal_continuation=terminal_continuation,
                source_descriptor=source_descriptor,
            )
        )
    finally:
        os.close(source_descriptor)
    if schema_version == 1:
        record, payloads = normalize_run(observed)
    elif schema_version == 2:
        record, payloads = normalize_run_v2(observed)
    else:
        record, payloads = normalize_run_v3(observed)
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


def load_source_v2(
    source,
    project,
    run_id,
    bead_id,
    terminal_continuation=None,
    source_descriptor=None,
):
    """Load a terminal Coordinator, Preflight, or Acceptance Routing Run."""
    preparation_path = source / "preparation.json"
    if not preparation_path.exists() and not preparation_path.is_symlink():
        observed = load_source(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
            source_descriptor=source_descriptor,
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
        return load_terminal_routing(
            source,
            preparation,
            project,
            run_id,
            bead_id,
            source_descriptor=source_descriptor,
        )
    if preparation_status != "paused":
        if "preflight" in preparation:
            require_directory(source / "preflight")
        observed = load_source(
            source,
            project,
            run_id,
            bead_id,
            terminal_continuation=terminal_continuation,
            source_descriptor=source_descriptor,
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
    if not isinstance(preparation, dict) or set(preparation) not in (
        required,
        required | {"related_work"},
    ):
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
    related_work = load_related_work(source, preparation, assignment, request)
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
        "related_work": related_work,
    }


def load_terminal_routing(
    source,
    preparation,
    project,
    run_id,
    bead_id,
    source_descriptor=None,
):
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
        or set(preparation) not in (expected, expected | {"related_work"})
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
    related_work = load_related_work(source, preparation, assignment, request)
    if assignment.get("source") != {"kind": "bead", "id": bead["id"]}:
        raise ExportError("Acceptance Routing Bead identity disagrees")
    routing = validate_prepared_routing(
        source, preparation["routing"], source_descriptor=source_descriptor
    )
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
        "related_work": related_work,
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
    source_descriptor=None,
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
        related_work = load_related_work(
            source, preparation, root_assignment, root_request
        )
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
                source,
                preparation["routing"],
                source_descriptor=source_descriptor,
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
        related_work = None

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
        "preparation": preparation,
        "related_work": related_work,
    }


def load_related_work(source, preparation, assignment, request):
    """Validate one immutable snapshot binding across all prepared Run records."""
    references = (
        preparation.get("related_work"),
        assignment.get("related_work"),
        request.get("related_work"),
    )
    if references == (None, None, None):
        return None
    if any(item is None for item in references) or not (
        references[0] == references[1] == references[2]
    ):
        raise ExportError("prepared related-work references disagree")
    try:
        validate_reference(references[0], expected_path=source / SNAPSHOT_NAME)
        raw = validate_snapshot(source / SNAPSHOT_NAME, references[0])
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExportError("invalid prepared related-work snapshot") from error
    return {"reference": references[0], "raw": raw}


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
            frozenset(expected | {"related_work"}),
            frozenset(expected | {"routing", "related_work"}),
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


def validate_prepared_routing(source, prepared, source_descriptor=None):
    """Validate and retain the complete, contract-bound v2 routing stage."""
    # Duplicate the export's initially validated Run descriptor when available.
    # Reopening source by pathname would allow a Run or ancestor replacement to
    # redirect all otherwise descriptor-relative routing reads.
    descriptors = []
    try:
        routing_source_descriptor = (
            os.open(source, DIRECTORY_FLAGS)
            if source_descriptor is None
            else os.dup(source_descriptor)
        )
        descriptors.append(routing_source_descriptor)
        planner_descriptor = os.open(
            "planner", DIRECTORY_FLAGS, dir_fd=routing_source_descriptor
        )
        descriptors.append(planner_descriptor)
        policy_descriptor = os.open(
            "policy", DIRECTORY_FLAGS, dir_fd=routing_source_descriptor
        )
        descriptors.append(policy_descriptor)

        planner_input = validate_plan_input(
            read_json_at(routing_source_descriptor, "planner-input.json")
        )
        planner_raw = read_bytes_at(planner_descriptor, "output.json", MAX_JSON_BYTES)
        planner_output = validate_planner_output(
            planner_input, json.loads(decode_text(planner_raw))
        )
        evidence_name = "routing" if planner_output["plan"] is None else "plan"
        evidence = planner_output[evidence_name]
        policy_input = read_json_at(routing_source_descriptor, "policy-input.json")
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


def normalize_run_v2(observed, include_artifacts=True):
    """Create the v2 semantic record and, when requested, public artifacts."""
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

    if not include_artifacts:
        return record, {}
    descriptors, payloads = public_artifacts(observed)
    record["artifacts"] = descriptors
    return record, payloads


def normalize_run_v3(observed):
    """Create the v3 Run record with one sanitized copy per step object."""
    # v3 has a different artifact catalog.  Do not perform the v2 admission
    # pass first: besides reading everything twice, that pass can consume its
    # budget before reaching required related-work evidence.
    record, _ = normalize_run_v2(observed, include_artifacts=False)
    candidates = artifact_candidates(observed)
    descriptors, payloads = public_artifacts(
        observed, candidates=artifact_candidates_v3(observed, candidates)
    )
    record["schema_version"] = 3
    record["artifacts"] = descriptors
    sessions = inference_sessions_v3(candidates)
    if sessions:
        record["inference_sessions"] = sessions
    return record, payloads


def inference_sessions_v3(candidates):
    """Keep receipt-authenticated status metadata out of the Artifact catalog."""
    terminal_attempts = {}
    for candidate in candidates:
        if candidate["kind"] != "inference_terminal_response_view":
            continue
        value = json.loads(decode_text(candidate["generated_raw"]))
        directory = (
            candidate["destination"]
            .removesuffix("/views/terminal-response.json")
            .removeprefix("artifacts/")
        )
        terminal_attempts[(candidate["scope"], directory)] = value["attempt_number"]

    sessions = []
    for candidate in candidates:
        if candidate["kind"] != "inference_receipt_view":
            continue
        value = json.loads(decode_text(candidate["generated_raw"]))
        directory = candidate["source"].removesuffix("/receipt.json")
        terminal_attempt = terminal_attempts.get((candidate["scope"], directory))
        attempts = [
            {
                **attempt,
                "terminal": attempt["attempt_number"] == terminal_attempt,
            }
            for attempt in value["attempts"]
        ]
        session = {
            "scope": candidate["scope"],
            "directory": directory,
            "identity": value["identity"],
            "requested_capability": value["requested_capability"],
            "duration_seconds": value["duration_seconds"],
            "attempt_count": value["attempt_count"],
            "attempts": attempts,
            "validation_status": value["validation_status"],
        }
        sessions.append(sanitize_secret_json_value(session, frozenset())[0])
    return sessions


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
        "criteria": [
            {
                "id": criterion["id"],
                "statement": sanitize_public_text(criterion["statement"], redactions)[
                    :2048
                ],
            }
            for criterion in routing["criteria"]
        ],
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
        "criteria",
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


def public_artifacts(observed, candidates=None):
    candidates = artifact_candidates(observed) if candidates is None else candidates
    descriptors = []
    payloads = {}
    # Structured records and human-readable logs are admitted before event
    # streams.  Stable sorting makes the policy independent of filesystem order.
    candidates.sort(key=lambda item: (item["priority"], item["source"]))
    budget = V2_MAX_BUNDLE_BYTES - MAX_MANIFEST_BYTES - MAX_INCLUDED_BYTES
    file_budget = MAX_BUNDLE_FILES - 1  # workflow-run.json occupies one slot
    used = 0

    # Load and admit required related-work before considering optional evidence.
    # Keep descriptor ordering stable, but reserve both its bytes and file slot
    # now so an earlier-sorting component or inference artifact cannot crowd it
    # out.  Reading it first also preserves the existing fail-closed race and
    # validation behavior.
    required = {}
    for candidate in candidates:
        if candidate.get("kind") != "related_work":
            continue
        descriptor, data = derive_public_artifact(candidate, observed["redactions"])
        required[id(candidate)] = descriptor, data
        if data is None:
            continue
        if used + len(data) > budget or len(payloads) >= file_budget:
            raise ExportError(
                "validated related-work snapshot cannot be published: bundle_limit"
            )
        if descriptor["path"] in payloads:
            raise ExportError("public artifact destinations collide")
        used += len(data)
        payloads[descriptor["path"]] = data

    for candidate in candidates:
        if id(candidate) in required:
            descriptor, data = required[id(candidate)]
            descriptors.append(descriptor)
            continue
        descriptor, data = derive_public_artifact(candidate, observed["redactions"])
        if data is not None and (
            used + len(data) > budget or len(payloads) >= file_budget
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
        expected_sha256=None,
        inference_view=False,
        private_source=False,
        generated_raw=None,
        unsafe_generated=False,
        destination=None,
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
            inference_view,
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
                "expected_sha256": expected_sha256,
                "inference_view": inference_view,
                "private_source": private_source,
                "generated_raw": generated_raw,
                "unsafe_generated": unsafe_generated,
                **({"destination": destination} if destination is not None else {}),
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
        if observed.get("related_work"):
            add(
                SNAPSHOT_NAME,
                "run",
                "related_work",
                observed["related_work"]["reference"]["media_type"],
                0,
                validated_raw=observed["related_work"]["raw"],
            )
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
        inference_relative = "planner/inference"
        if (root / inference_relative).exists() or (
            root / inference_relative
        ).is_symlink():
            for item in receipt_bound_inference_artifacts(
                root,
                inference_relative,
                "acceptance_planning",
                None,
            ):
                add(**item)
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
            inference_relative = f"{base}/inference"
            if (root / inference_relative).exists() or (
                root / inference_relative
            ).is_symlink():
                inference_purpose = {
                    "assessment": "finding_assessment",
                    "response": "feedback_response",
                }.get(entry["component"], entry["component"])
                for item in receipt_bound_inference_artifacts(
                    root,
                    inference_relative,
                    inference_purpose,
                    None,
                ):
                    add(**item)
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
        if (
            not candidate["unsafe_path"]
            and source_counts[candidate["source"]] > 1
            and "destination" not in candidate
        ):
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


def artifact_candidates_v3(observed, originals=None):
    """Select inspectable step objects without v2 private/view descriptor pairs."""
    selected = []
    for original in (
        originals if originals is not None else artifact_candidates(observed)
    ):
        candidate = original.copy()
        candidate["secrets_only"] = True
        if (
            candidate["kind"] == "events"
            and candidate["scope"].startswith("component:")
            and candidate["scope"].endswith(":attempt")
        ):
            # Attempt stdout is private session evidence.  Keep a visible
            # nondownloadable source declaration and publish a separately
            # derived, output-bound transcript under the Attempt's own scope.
            candidate.update(
                kind="attempt_events_private",
                private_source=False,
                media_type="application/x-ndjson",
            )
            candidate.pop("destination", None)
            selected.append(candidate)
            transcript = original.copy()
            transcript.update(
                kind="attempt_session_transcript",
                media_type="application/json",
                priority=0,
                private_source=False,
                inference_view=False,
                generated_raw=None,
                secrets_only=False,
            )
            transcript.pop("destination", None)
            selected.append(transcript)
        elif candidate["kind"] == "inference_prompt":
            # The Receipt-bound prompt remains integrity evidence.  Operators
            # receive only the independently sanitized section artifacts below.
            candidate.update(
                private_source=True,
                inference_view=False,
                generated_raw=None,
            )
            candidate.pop("destination", None)
            selected.append(candidate)
        elif candidate["kind"] == "inference_response":
            candidate.update(
                kind="json",
                media_type="application/json",
                private_source=False,
                inference_view=False,
                generated_raw=None,
                expected_sha256=None,
            )
            candidate.pop("destination", None)
            selected.append(candidate)
        elif candidate["kind"] in {
            "inference_system_instructions",
            "inference_task_instructions",
            "inference_task_data",
        }:
            # Prompt sections are separate operator-owned objects.  Do not let
            # v3's ordinary shape-preserving secret-only policy bypass their
            # stricter host-path sanitation.
            candidate["secrets_only"] = False
            selected.append(candidate)
        elif (
            candidate["kind"].startswith("inference_")
            or candidate["source"] == "preflight-input.json"
        ):
            continue
        else:
            candidate.pop("destination", None)
            selected.append(candidate)

    root = observed["run_root"]
    if observed.get("acceptance_routing"):
        for source, kind, media_type, priority in (
            ("planner/input.json", "json", "application/json", 0),
            ("policy/input.json", "json", "application/json", 0),
            ("planner/stderr.log", "log", "text/plain; charset=utf-8", 1),
            ("planner/events.jsonl", "events", "application/x-ndjson", 2),
        ):
            selected.append(
                {
                    "root": root,
                    "source": source,
                    "scope": "acceptance_routing",
                    "kind": kind,
                    "media_type": media_type,
                    "priority": priority,
                    "unsafe_path": False,
                    "declaration": None,
                    "validated_preflight_classifier_key": None,
                    "validated_preflight_output_raw": None,
                    "validated_raw": None,
                    "expected_sha256": None,
                    "inference_view": False,
                    "private_source": False,
                    "generated_raw": None,
                    "secrets_only": True,
                }
            )

    destinations = set()
    for candidate in selected:
        if candidate["unsafe_path"]:
            continue
        desired = candidate.get("destination") or (
            f"artifacts/{candidate['source'].removesuffix('events.jsonl')}session-transcript.json"
            if candidate["kind"] == "attempt_session_transcript"
            else f"artifacts/{candidate['source']}"
        )
        destination = desired
        duplicate = 2
        while destination in destinations:
            destination = f"{desired}.duplicate-{duplicate}"
            duplicate += 1
        candidate["destination"] = destination
        destinations.add(destination)
    return selected


def receipt_bound_inference_artifacts(root, relative, purpose, expected_setting=None):
    """Authenticate one runtime evidence directory and return its closed catalog."""
    try:
        directory_descriptor = open_directory_beneath(root, relative)
    except OSError as error:
        raise ExportError("invalid Inference Receipt evidence") from error
    try:
        return _receipt_bound_inference_artifacts(
            root, relative, purpose, expected_setting, directory_descriptor
        )
    finally:
        os.close(directory_descriptor)


def _receipt_bound_inference_artifacts(
    root, relative, purpose, expected_setting, directory_descriptor
):
    # prompt.json contains three independently limited public artifacts and is
    # repeated by invocation.json. Account for JSON escaping while retaining a
    # finite private-envelope read bound.
    prompt_envelope_limit = V2_MAX_ARTIFACT_BYTES * 20 + 64 * 1024
    try:
        receipt_raw = read_bytes_at(
            directory_descriptor, "receipt.json", prompt_envelope_limit
        )
        receipt = json.loads(decode_text(receipt_raw))
        invocation_raw = read_bytes_at(
            directory_descriptor, "invocation.json", prompt_envelope_limit
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExportError("invalid Inference Receipt evidence") from error

    if not isinstance(receipt, dict):
        raise ExportError("invalid Inference Receipt evidence")
    identity = receipt.get("identity")
    hashes = receipt.get("hashes")
    if not isinstance(hashes, dict):
        raise ExportError("Inference Receipt private source identity disagrees")
    invocation_hash = hashes.get("invocation_sha256")
    if invocation_hash is None:
        raise ExportError("Inference Receipt omits its invocation hash")
    if not isinstance(invocation_hash, str) or not SHA256_TEXT.fullmatch(
        invocation_hash
    ):
        raise ExportError("invalid Inference Receipt artifact hash")
    if digest(invocation_raw) != invocation_hash:
        raise ExportError("Inference Receipt artifact hash disagrees")
    try:
        invocation = json.loads(decode_text(invocation_raw))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExportError("invalid Inference Receipt evidence") from error
    if not isinstance(invocation, dict):
        raise ExportError("invalid Inference Receipt evidence")
    adapter = invocation.get("adapter")
    model = identity.get("model") if isinstance(identity, dict) else None
    thinking = identity.get("thinking") if isinstance(identity, dict) else None
    if (
        receipt.get("schema_version") != 1
        or not isinstance(identity, dict)
        or not isinstance(model, str)
        or not model.strip()
        or not isinstance(thinking, str)
        or thinking not in SUPPORTED_THINKING
        or identity.get("runtime") != "afk-inference-v1"
        or identity.get("adapter") != "pi-v1"
        or identity.get("adapter_family") != "pi"
        or type(identity.get("adapter_contract_version")) is not int
        or identity.get("adapter_contract_version") != 1
        or not isinstance(adapter, dict)
        or set(adapter)
        != {
            "kind",
            "family",
            "contract_version",
            "identity",
            "model",
            "thinking",
            "capabilities",
        }
        or adapter.get("kind") != "pi"
        or adapter.get("family") != "pi"
        or type(adapter.get("contract_version")) is not int
        or adapter.get("contract_version") != 1
        or adapter.get("identity") != identity.get("adapter")
        or adapter.get("model") != identity.get("model")
        or adapter.get("thinking") != identity.get("thinking")
        or adapter.get("capabilities") != ["NO_TOOLS", "READ_ONLY", "WRITE"]
        or invocation.get("schema_version") != 1
        or invocation.get("purpose") != purpose
        or invocation.get("evidence_directory") != str(root.resolve() / relative)
    ):
        raise ExportError("Inference Receipt private source identity disagrees")

    if expected_setting is not None and (
        identity.get("adapter_family") != expected_setting["adapter_family"]
        or identity.get("adapter_contract_version")
        != expected_setting["adapter_contract_version"]
        or model != expected_setting["model"]
        or thinking != expected_setting["thinking"]
    ):
        raise ExportError("Inference Receipt disagrees with frozen role policy")

    allowed_hashes = {
        "invocation_sha256",
        "prompt_sha256",
        "task_prompt_sha256",
        "adapter_contract_sha256",
    }
    if any(
        name.endswith("_sha256") and name not in allowed_hashes and value is not None
        for name, value in hashes.items()
    ):
        raise ExportError("Inference Receipt names an unknown artifact hash")

    catalog = []

    def bind(path, claimed_hash, kind, media_type, priority, read_limit=None):
        if claimed_hash is None:
            return
        if not isinstance(claimed_hash, str) or not SHA256_TEXT.fullmatch(claimed_hash):
            raise ExportError("invalid Inference Receipt artifact hash")
        if not safe_relative(path) or not path.startswith(relative + "/"):
            raise ExportError("invalid Inference Receipt artifact identity")
        local_path = path[len(relative) + 1 :]
        try:
            raw = read_bytes_beneath(
                directory_descriptor,
                local_path,
                V2_MAX_ARTIFACT_BYTES if read_limit is None else read_limit,
            )
        except (OSError, ExportError) as error:
            raise ExportError("Inference Receipt artifact is unavailable") from error
        if digest(raw) != claimed_hash:
            raise ExportError("Inference Receipt artifact hash disagrees")
        catalog.append(
            {
                "relative": path,
                "scope": f"inference:{purpose}",
                "kind": kind,
                "media_type": media_type,
                "priority": priority,
                "validated_raw": raw,
                "expected_sha256": claimed_hash,
                "private_source": True,
            }
        )
        return raw

    # Reading either envelope under one artifact's public-byte limit would let
    # one oversized section suppress the other section descriptors.
    bind(
        f"{relative}/invocation.json",
        hashes.get("invocation_sha256"),
        "inference_invocation",
        "application/json",
        0,
        prompt_envelope_limit,
    )
    prompt_raw = bind(
        f"{relative}/prompt.json",
        hashes.get("prompt_sha256"),
        "inference_prompt",
        "application/json",
        0,
        prompt_envelope_limit,
    )
    if prompt_raw is None:
        raise ExportError("Inference Receipt omits its prompt hash")
    try:
        prompt = json.loads(decode_text(prompt_raw))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExportError("invalid Inference Receipt prompt") from error
    if not isinstance(prompt, dict):
        raise ExportError("invalid Inference Receipt prompt")
    system_instructions = prompt.get("system")
    task_instructions = prompt.get("trusted_task_instructions", prompt.get("task"))
    if not isinstance(system_instructions, str) or not isinstance(
        task_instructions, str
    ):
        raise ExportError("invalid Inference Receipt prompt sections")
    policy = receipt.get("policy")
    if (
        invocation.get("prompt") != prompt
        or not isinstance(policy, dict)
        or policy.get("system_instructions") != system_instructions
        or policy.get("requested_capability") != invocation.get("requested_capability")
    ):
        raise ExportError("Inference Receipt prompt identity disagrees")
    # A prompt is one private integrity object but three independently useful
    # operator objects.  Give every section its own identity and sanitation
    # decision so an unsafe or oversized section cannot suppress, expose, or
    # mislabel either of its siblings.
    for kind, media_type, value, filename in (
        (
            "inference_system_instructions",
            "text/plain; charset=utf-8",
            system_instructions,
            "system-instructions.txt",
        ),
        (
            "inference_task_instructions",
            "text/plain; charset=utf-8",
            task_instructions,
            "task-instructions.txt",
        ),
        (
            "inference_task_data",
            "application/json",
            prompt.get("untrusted_task_data"),
            "task-data.json",
        ),
    ):
        try:
            public_raw = (
                encode_json(value)
                if media_type == "application/json"
                else value.encode()
            )
        except UnicodeEncodeError:
            # json.loads deliberately permits escaped unpaired surrogates.
            # Treat a malformed text section as its own unsafe object rather
            # than aborting the catalog or borrowing bytes from prompt.json.
            public_raw = None
            unsafe_generated = True
        else:
            unsafe_generated = False
        catalog.append(
            {
                "relative": f"{relative}/prompt.json",
                "scope": f"inference:{purpose}",
                "kind": kind,
                "media_type": media_type,
                "priority": 0,
                "validated_raw": prompt_raw,
                "expected_sha256": hashes["prompt_sha256"],
                "generated_raw": public_raw,
                "unsafe_generated": unsafe_generated,
                "inference_view": True,
                "destination": f"artifacts/{relative}/views/{filename}",
            }
        )
    contract_name = "adapter-contract.json"
    contract_hash = hashes.get("adapter_contract_sha256")
    if "adapter_script_sha256" in hashes:
        raise ExportError("Inference Receipt names a non-production adapter")
    if contract_hash is None:
        raise ExportError("Inference Receipt omits its adapter contract hash")
    contract_raw = bind(
        f"{relative}/{contract_name}",
        contract_hash,
        "inference_contract",
        "application/json",
        0,
    )
    try:
        contract = json.loads(decode_text(contract_raw))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExportError("invalid Inference Receipt adapter contract") from error
    # The runtime writes the same closed descriptor into invocation.json and the
    # separately hashed contract. Equality binds family, version, identity,
    # model, thinking, and the complete capability set to one source identity.
    if not isinstance(contract, dict) or contract != adapter:
        raise ExportError("Inference Receipt adapter contract identity disagrees")
    if hashes.get("task_prompt_sha256") is not None:
        task_prompt_raw = bind(
            f"{relative}/task-prompt.txt",
            hashes["task_prompt_sha256"],
            "inference_prompt_text",
            "text/plain; charset=utf-8",
            0,
        )
        task_prompt = prompt.get("task_prompt")
        if not isinstance(task_prompt, str) or task_prompt_raw != task_prompt.encode():
            raise ExportError("Inference Receipt task prompt identity disagrees")

    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or receipt.get("attempt_count") != len(attempts):
        raise ExportError("invalid Inference Receipt attempt catalog")
    response_records = []
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict):
            raise ExportError("invalid Inference Receipt attempt identity")
        artifacts = attempt.get("artifacts")
        if attempt.get("attempt_number") != index or not isinstance(artifacts, dict):
            raise ExportError("invalid Inference Receipt attempt identity")
        expected = {
            "events": (
                f"attempts/{index}/events.jsonl",
                "inference_events",
                "application/x-ndjson",
                2,
            ),
            "stderr": (
                f"attempts/{index}/stderr.log",
                "inference_log",
                "text/plain; charset=utf-8",
                1,
            ),
            "response": (
                f"attempts/{index}/response.json",
                "inference_response",
                "application/json",
                0,
            ),
        }
        allowed_attempt_hashes = {f"{name}_sha256" for name in expected}
        if any(
            name.endswith("_sha256")
            and name not in allowed_attempt_hashes
            and value is not None
            for name, value in artifacts.items()
        ):
            raise ExportError("Inference Receipt names an unknown attempt hash")
        response_raw = None
        response_hash = None
        for name, (expected_path, kind, media, priority) in expected.items():
            path = artifacts.get(name)
            claimed = artifacts.get(f"{name}_sha256")
            if path is None and claimed is None:
                continue
            if path != expected_path:
                raise ExportError(
                    "Inference Receipt attempt artifact identity disagrees"
                )
            bound_raw = bind(f"{relative}/{path}", claimed, kind, media, priority)
            if name == "response":
                response_raw, response_hash = bound_raw, claimed
        response_value = None
        response_available = response_raw is not None
        if response_available:
            try:
                response_value = json.loads(decode_text(response_raw))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ExportError("invalid Inference Receipt response") from error
        response_records.append(
            {
                "attempt_number": index,
                "protocol": attempt.get("protocol"),
                "response": response_value,
                "available": response_available,
                "raw": response_raw,
                "hash": response_hash,
                "source": f"{relative}/attempts/{index}/response.json",
            }
        )

    terminal_value = receipt.get("terminal_response")
    terminal_record = None
    # The runtime identifies the response it processed through validation's
    # attempt number. Value equality is only an integrity check: it cannot
    # identify a terminal attempt when two attempts emitted equal JSON, and a
    # valid JSON null response cannot be distinguished by value from no response.
    validation = receipt.get("validation")
    terminal_attempt = (
        validation.get("attempt_number") if isinstance(validation, dict) else None
    )
    if terminal_attempt is not None:
        if (
            not isinstance(terminal_attempt, int)
            or isinstance(terminal_attempt, bool)
            or terminal_attempt < 1
            or terminal_attempt > len(response_records)
        ):
            raise ExportError(
                "Inference Receipt terminal response lacks attempt identity"
            )
        terminal_record = response_records[terminal_attempt - 1]
        attempt = attempts[terminal_attempt - 1]
        attempt_protocol = attempt.get("protocol")
        if (
            not terminal_record["available"]
            or terminal_record["response"] != terminal_value
            or attempt.get("validation") != validation
            or not isinstance(attempt_protocol, dict)
            or attempt_protocol.get("status") != "accepted"
        ):
            raise ExportError(
                "Inference Receipt terminal response is not attempt-bound"
            )
    elif terminal_value is not None:
        raise ExportError("Inference Receipt terminal response lacks attempt identity")
    else:
        # A null terminal value can mean either no response or an accepted JSON
        # null response. Accepted protocol/validation state must therefore carry
        # an attempt number rather than silently degrading to an unavailable view.
        accepted_without_identity = any(
            isinstance(record, dict) and record.get("status") == "accepted"
            for record in (
                receipt.get("protocol"),
                validation,
                *(attempt.get("protocol") for attempt in attempts),
                *(attempt.get("validation") for attempt in attempts),
            )
        )
        if accepted_without_identity:
            raise ExportError(
                "Inference Receipt terminal response lacks attempt identity"
            )

    for item in response_records:
        view = encode_json(
            {
                "schema_version": 1,
                "kind": "inference_response_view",
                "attempt_number": item["attempt_number"],
                "protocol": item["protocol"],
                "available": item["available"],
                "terminal": item is terminal_record,
                "response": item["response"],
            }
        )
        catalog.append(
            {
                "relative": item["source"],
                "scope": f"inference:{purpose}",
                "kind": "inference_response_view",
                "media_type": "application/json",
                "priority": 0,
                "validated_raw": item["raw"],
                "expected_sha256": item["hash"],
                "generated_raw": view,
                "inference_view": True,
                "destination": (
                    f"artifacts/{relative}/views/responses/"
                    f"{item['attempt_number']}.json"
                ),
            }
        )

    terminal_view = encode_json(
        {
            "schema_version": 1,
            "kind": "inference_terminal_response_view",
            "attempt_number": (
                terminal_record["attempt_number"] if terminal_record else None
            ),
            "available": terminal_record is not None,
            "response": terminal_record["response"] if terminal_record else None,
        }
    )
    terminal_source = (
        terminal_record["source"]
        if terminal_record is not None
        else f"{relative}/receipt.json"
    )
    catalog.append(
        {
            "relative": terminal_source,
            "scope": f"inference:{purpose}",
            "kind": "inference_terminal_response_view",
            "media_type": "application/json",
            "priority": 0,
            "validated_raw": (
                terminal_record["raw"] if terminal_record is not None else receipt_raw
            ),
            "expected_sha256": (
                terminal_record["hash"]
                if terminal_record is not None
                else digest(receipt_raw)
            ),
            "generated_raw": terminal_view,
            "inference_view": True,
            "destination": f"artifacts/{relative}/views/terminal-response.json",
        }
    )

    validation_status = (
        validation.get("status") if isinstance(validation, dict) else None
    )
    if not isinstance(validation_status, str):
        raise ExportError("Inference Receipt validation status is invalid")
    public_attempts = []
    for attempt in attempts:
        attempt_protocol = attempt.get("protocol")
        attempt_validation = attempt.get("validation")
        protocol_status = (
            attempt_protocol.get("status")
            if isinstance(attempt_protocol, dict)
            else None
        )
        attempt_validation_status = (
            attempt_validation.get("status")
            if isinstance(attempt_validation, dict)
            else None
        )
        if not isinstance(protocol_status, str) or (
            attempt_validation_status is not None
            and not isinstance(attempt_validation_status, str)
        ):
            raise ExportError("Inference Receipt attempt status is invalid")
        public_attempts.append(
            {
                "attempt_number": attempt["attempt_number"],
                "protocol_status": protocol_status,
                "validation_status": attempt_validation_status,
            }
        )
    receipt_view = encode_json(
        {
            "schema_version": 1,
            "kind": "inference_receipt_view",
            "identity": {
                name: identity[name]
                for name in (
                    "runtime",
                    "adapter",
                    "adapter_family",
                    "adapter_contract_version",
                    "model",
                    "thinking",
                )
            },
            "requested_capability": policy["requested_capability"],
            "duration_seconds": receipt["timing"]["duration_seconds"],
            "attempt_count": receipt["attempt_count"],
            "attempts": public_attempts,
            "validation_status": validation_status,
        }
    )
    catalog.append(
        {
            "relative": f"{relative}/receipt.json",
            "scope": f"inference:{purpose}",
            "kind": "inference_receipt_view",
            "media_type": "application/json",
            "priority": 0,
            "validated_raw": receipt_raw,
            "expected_sha256": digest(receipt_raw),
            "generated_raw": receipt_view,
            "inference_view": True,
            "destination": f"artifacts/{relative}/views/receipt.json",
        }
    )

    # The receipt is the authority for the other entries. Retain its exact
    # private identity even though a receipt cannot recursively hash itself.
    catalog.append(
        {
            "relative": f"{relative}/receipt.json",
            "scope": f"inference:{purpose}",
            "kind": "inference_receipt",
            "media_type": "application/json",
            "priority": 0,
            "validated_raw": receipt_raw,
            "expected_sha256": digest(receipt_raw),
            "private_source": True,
        }
    )
    return catalog


def derive_public_artifact(candidate, redactions):
    source = candidate["source"]
    source_identity = {"path": source}
    if candidate.get("expected_sha256") is not None:
        source_identity.update(
            bytes=len(candidate["validated_raw"]),
            sha256=candidate["expected_sha256"],
        )
    base = {
        "source": source_identity,
        "scope": candidate["scope"],
        "kind": candidate["kind"],
        "media_type": candidate["media_type"],
    }

    def unavailable(state, reason):
        # Unlike ordinary evidence, the frozen related-work snapshot is a
        # required, already-validated Run artifact.  Publication must fail
        # closed if a race prevents emitting those exact bytes.
        if candidate["kind"] == "related_work" and not candidate.get("secrets_only"):
            raise ExportError(
                f"validated related-work snapshot cannot be published: {reason}"
            )
        return nondownloadable_descriptor(base, state, reason), None

    if candidate.get("unsafe_path"):
        return unavailable("unsafe", "unsafe_path")
    if candidate.get("private_source"):
        return nondownloadable_descriptor(base, "unavailable", "private_source"), None
    if candidate.get("unsafe_generated"):
        return unavailable("unsafe", "unsafe_or_invalid")
    generated = candidate.get("generated_raw")
    if generated is not None:
        raw = generated
        # Generated views have already been isolated from their private source;
        # classify their own byte limit before running potentially expensive
        # text and path sanitation.
        if len(raw) > V2_MAX_ARTIFACT_BYTES:
            return unavailable("oversized", "artifact_limit")
    else:
        path = candidate["root"] / source
        try:
            facts = path.lstat()
        except FileNotFoundError:
            return unavailable("unavailable", "missing")
        except (OSError, ValueError):
            return unavailable("unavailable", "unavailable")
        if stat.S_ISLNK(facts.st_mode) or not stat.S_ISREG(facts.st_mode):
            return unavailable("unsafe", "unsafe_file")
        if facts.st_size == 0 and candidate["kind"] != "attempt_session_transcript":
            return unavailable("empty", "empty")
        if facts.st_size > V2_MAX_ARTIFACT_BYTES:
            return unavailable("oversized", "artifact_limit")
        try:
            raw = read_bytes(path, V2_MAX_ARTIFACT_BYTES, expected_facts=facts)
        except (ExportError, OSError):
            # Optional evidence remains describable, but required frozen context
            # cannot degrade into a nondownloadable artifact after source loading.
            return unavailable("unavailable", "unavailable")
    if candidate["kind"] == "attempt_events_private":
        return unavailable("unsafe", "private_attempt_events")
    json_sanitizer = (
        sanitize_secret_json_value
        if candidate.get("secrets_only")
        else sanitize_json_value
    )
    if (
        candidate["kind"] == "related_work"
        and generated is None
        and candidate.get("validated_raw") is not None
        and raw != candidate["validated_raw"]
    ):
        raise ExportError("validated related-work snapshot changed")
    try:
        validated_preflight_output_raw = candidate.get("validated_preflight_output_raw")
        if (
            validated_preflight_output_raw is not None
            and raw != validated_preflight_output_raw
        ):
            raise ExportError("validated Preflight output changed")
        if (
            generated is None
            and candidate.get("validated_raw") is not None
            and raw != candidate["validated_raw"]
        ):
            raise ExportError("validated Acceptance Routing output changed")
        text = decode_text(raw)
        if candidate["kind"] == "attempt_session_transcript":
            value = build_attempt_transcript(
                raw,
                lambda item: sanitize_public_artifact_text(item, redactions),
            )
            public = encode_attempt_transcript(value)
            changed = True
        elif candidate.get("inference_view"):
            view = (
                json.loads(text)
                if candidate["media_type"] == "application/json"
                else text
            )
            if inference_view_contains_host_reference(view, redactions):
                raise ExportError("inference view contains an unknown host path")
        if candidate["kind"] == "attempt_session_transcript":
            pass
        elif candidate["kind"] == "related_work" and not candidate.get("secrets_only"):
            if candidate.get("validated_raw") != raw:
                raise ExportError("validated related-work snapshot changed")
            public = raw
            changed = False
        elif candidate["kind"] in {"json", "planner", "policy"} | INFERENCE_JSON_KINDS:
            value = json.loads(text)
            changed = sanitize_validated_preflight_classifier_key(
                value, candidate.get("validated_preflight_classifier_key")
            )
            value, generally_changed = json_sanitizer(value, redactions)
            changed = changed or generally_changed
            public = encode_json(value)
        elif candidate["kind"] in {"events", "inference_events"} or (
            candidate["kind"] == "related_work" and candidate.get("secrets_only")
        ):
            lines = []
            changed = False
            for line in text.splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                value, item_changed = json_sanitizer(value, redactions)
                changed = changed or item_changed
                lines.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
            if not lines:
                return nondownloadable_descriptor(base, "empty", "empty"), None
            public = ("\n".join(lines) + "\n").encode()
        else:
            sanitized = (
                sanitize_secret_text(text)
                if candidate.get("secrets_only")
                else sanitize_public_artifact_text(text, redactions)
            )
            changed = sanitized != text
            public = sanitized.encode()
    except ExportError:
        if candidate["kind"] == "related_work" and not candidate.get("secrets_only"):
            raise
        return nondownloadable_descriptor(base, "unsafe", "unsafe_or_invalid"), None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return unavailable("unsafe", "unsafe_or_invalid")
    if len(public) > V2_MAX_ARTIFACT_BYTES:
        return unavailable("oversized", "artifact_limit")
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


def sensitive_json_key(value):
    """Recognize sensitive compound keys in separated and CamelCase forms."""
    separated = JSON_CAMEL_BOUNDARY.sub("_", value)
    return bool(SENSITIVE_JSON_KEY.fullmatch(separated))


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
            if sensitive_json_key(key) and value[key] is not None:
                public = REDACTED_SECRET
                item_changed = value[key] != REDACTED_SECRET
            else:
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


def sanitize_secret_json_value(value, _redactions):
    """Preserve v3 JSON shape and content except recognized secret material."""
    if isinstance(value, str):
        option = CREDENTIAL_OPTION_VALUE.fullmatch(value)
        if option:
            return f"{option.group(1)}={REDACTED_SECRET}", True
        public = sanitize_secret_text(value)
        return public, public != value
    if isinstance(value, list):
        result, changed = [], False
        redact_next = False
        for item in value:
            if redact_next and isinstance(item, str):
                public, item_changed = REDACTED_SECRET, item != REDACTED_SECRET
            else:
                public, item_changed = sanitize_secret_json_value(item, _redactions)
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
            if sensitive_json_key(key) and value[key] is not None:
                public = REDACTED_SECRET
                item_changed = value[key] != REDACTED_SECRET
            else:
                public, item_changed = sanitize_secret_json_value(
                    value[key], _redactions
                )
            result[key] = public
            changed = changed or item_changed
        return result, changed
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportError("non-finite JSON number")
        return value, False
    raise ExportError("unsupported JSON value")


def sanitize_secret_text(text):
    """Remove recognized credentials from v3 text without rewriting other prose."""
    if PRIVATE_KEY_TEXT.search(text):
        raise ExportError("artifact contains unsafe private key material")
    for pattern in REDACTABLE_CREDENTIAL_TEXT:
        text = pattern.sub(REDACTED_SECRET, text)
    return text


def inference_view_contains_host_reference(value, redactions):
    """Reject a complete derived view when arbitrary prose contains a host path."""
    if isinstance(value, str):
        for prefix in sorted(redactions, key=len, reverse=True):
            value = value.replace(prefix, "[redacted-path]")
        return bool(ABSOLUTE_HOST_REFERENCE.search(value))
    if isinstance(value, list):
        return any(
            inference_view_contains_host_reference(item, redactions) for item in value
        )
    if isinstance(value, dict):
        return any(
            inference_view_contains_host_reference(key, redactions)
            or inference_view_contains_host_reference(item, redactions)
            for key, item in value.items()
        )
    return False


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


def component_contract_allows_null_agent(component, value):
    """Return whether this component envelope can legitimately omit an agent."""
    if component not in {"attempt", "review", "assessment", "response"}:
        return False
    if value["outcome"] != COMPONENT_TOPOLOGY[component]["success"]:
        return True
    # Response can complete without invoking an agent when there are no assessed
    # findings to address. Its producer records both the absent process and the
    # empty response, distinguishing that path from an accepted agent response.
    response = value.get("response")
    return (
        component == "response"
        and value.get("process") is None
        and isinstance(response, dict)
        and response.get("finding_responses") == []
    )


def normalize_component_agent(component, value, redactions):
    """Validate and normalize an agent envelope produced by a component."""
    agent = value["agent"]
    if agent is None:
        if not component_contract_allows_null_agent(component, value):
            raise ExportError("component contract does not permit a null agent")
        return None

    if component not in {"attempt", "review", "assessment", "response"}:
        raise ExportError("component contract does not permit agent facts")
    if not isinstance(agent, dict):
        raise ExportError("invalid agent facts")

    status = agent.get("status")
    if status == "error":
        if set(agent) != {"status", "error"} or not agent["error"]:
            raise ExportError("invalid agent facts")
        # Validate private error text even though the public representation only
        # exposes its category.
        bounded_text(agent["error"], redactions)
    elif status in {"completed", "aborted"}:
        if set(agent) != {"status"}:
            raise ExportError("invalid agent facts")
    else:
        raise ExportError("invalid agent facts")

    # Inference-backed components only record an agent after accepting its
    # response. Attempt additionally exposes aborted and error adapter states.
    if component != "attempt" and status != "completed":
        raise ExportError("invalid agent facts")
    if (
        value["outcome"] == COMPONENT_TOPOLOGY[component]["success"]
        and status != "completed"
    ):
        raise ExportError("successful component must have a completed agent")

    return {
        "status": status,
        **({"error_category": "protocol_error"} if status == "error" else {}),
    }


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
        # A permitted null agent is an observed fact: no response was accepted
        # (or required for the no-action Response path). Preserve that absence
        # rather than fabricating a terminal agent status.
        result["agent"] = normalize_component_agent(component, value, redactions)
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
        try:
            audit = validate_audit(review.get("audit"))
        except (TypeError, ValueError) as error:
            raise ExportError("invalid Review audit") from error
        return {
            "kind": "review",
            "summary": bounded_text(review.get("summary"), redactions),
            "findings": [
                normalize_finding(item, redactions) for item in review["findings"]
            ],
            "audit": audit,
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
    try:
        text = redact_public_paths(text, redactions)
    except ExportError as error:
        if str(error) != "text contains an ambiguously bounded host path":
            raise
        # Normalized Run prose is metadata, not a required downloadable
        # artifact. Preserve the Run while withholding a string whose path
        # boundary cannot be identified safely.
        text = "[redacted-unsafe-text]"
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

    # Ignore complete, safely bounded spaced paths while looking for the
    # ambiguous case. A whitespace-free HOST_PATH match is deliberately left
    # visible: it may be merely the leaked prefix of a spaced final component.
    ambiguity_input = list(text)
    for match in HOST_PATH.finditer(text):
        if re.search(r"[ \t]", match.group()):
            ambiguity_input[match.start() : match.end()] = " " * len(match.group())
    if AMBIGUOUS_SPACED_FINAL_PATH.search("".join(ambiguity_input)):
        raise ExportError("text contains an ambiguously bounded host path")

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
    return facts


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


def open_directory_beneath(root, relative):
    """Open a relative directory without following any path-component symlink."""
    if not safe_relative(relative):
        raise ExportError("Run path is not a safe relative directory")
    descriptor = os.open(root, DIRECTORY_FLAGS)
    try:
        for component in relative.split("/"):
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_bytes_beneath(directory_descriptor, name, limit, expected_facts=None):
    """Read a nested regular file without following intermediate symlinks."""
    if not safe_relative(name):
        raise ExportError("artifact path is not safe relative evidence")
    descriptor = os.dup(directory_descriptor)
    try:
        components = name.split("/")
        for component in components[:-1]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return read_bytes_at(descriptor, components[-1], limit, expected_facts)
    finally:
        os.close(descriptor)


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
