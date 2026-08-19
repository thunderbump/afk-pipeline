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
    validate_output,
    validate_request,
)
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
MAX_MANIFEST_BYTES = 64 * 1024
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
SENSITIVE_TEXT = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(
        r"(?i)(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AZURE_CLIENT_SECRET|"
        r"GOOGLE_APPLICATION_CREDENTIALS)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)(?:authorization\s*:\s*)?(?:basic|bearer)\s+\S{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
)
HOST_PATH = re.compile(
    r"(?<![A-Za-z0-9.])/(?:home|tmp|var|etc|opt|root|srv|mnt|usr|run|"
    r"Users|private|Library|Volumes|Applications|System)/[^\s'\"`]+"
)


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
    schema_version=1,
):
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
    if schema_version not in {1, 2}:
        raise ExportUsageError("unsupported Publication Bundle schema")
    observed = (
        load_source(source, project, run_id, bead_id)
        if schema_version == 1
        else load_source_v2(source, project, run_id, bead_id)
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


def load_source_v2(source, project, run_id, bead_id):
    """Load either a terminal Coordinator Run or a terminal Preflight pause."""
    preparation_path = source / "preparation.json"
    if not preparation_path.exists() and not preparation_path.is_symlink():
        observed = load_source(source, project, run_id, bead_id)
        observed["run_root"] = source
        observed["preflight"] = None
        return observed

    preparation = read_json(preparation_path)
    if not isinstance(preparation, dict):
        raise ExportError("invalid Run Preparer evidence")
    if preparation.get("preparation_status") != "paused":
        observed = load_source(source, project, run_id, bead_id)
        observed["run_root"] = source
        observed["preflight"] = load_optional_preflight(source, preparation)
        return observed

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


def load_optional_preflight(source, preparation):
    if "preflight" not in preparation:
        return None
    preflight_input = validate_preflight_input(
        read_json(source / "preflight-input.json")
    )
    invocation_input = validate_preflight_input(
        read_json(source / "preflight" / "input.json")
    )
    if invocation_input != preflight_input:
        raise ExportError("prepared Preflight inputs disagree")
    preflight_output = validate_preflight_output(
        read_json(source / "preflight" / "output.json"), preflight_input
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
    return {"input": preflight_input, "output": preflight_output}


def load_source(source, project, run_id, bead_id):
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

    state = validate_checkpoint(read_json(coordinator / "state.json"))
    output = validate_output(read_json(coordinator / "output.json"))
    if state["status"] == "running" or output != output_from_state(state):
        raise ExportError("Coordinator terminal evidence disagrees")
    if preparation is not None:
        validate_preparer_terminal(preparation, output)
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
        "redactions": {
            value
            for value in redactions
            if isinstance(value, str) and value.startswith("/")
        },
    }


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
        not in {frozenset(expected), frozenset(expected | {"preflight"})}
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
        preflight = sanitize_json_value(
            observed["preflight"]["output"], observed["redactions"]
        )[0]
        record = {
            "schema_version": 2,
            "identity": observed["identity"],
            "bead": {"id": observed["bead_id"]},
            "objective": bounded_text(
                observed["assignment"]["objective"], observed["redactions"]
            ),
            "response_limit": observed["request"]["max_responses"],
            "status": "paused",
            "terminal": {"stage": "preflight", "decision": "pause"},
            "history": [],
            "evidence": [],
            "preflight": {
                "outcome": preflight["outcome"],
                "decision": preflight["decision"],
                "requests": preflight["requests"],
            },
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

    descriptors, payloads = public_artifacts(observed)
    record["artifacts"] = descriptors
    return record, payloads


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
                state="unavailable",
                public_bytes=0,
                public_sha256=None,
                sanitization_status="not_applicable",
                unavailable_reason="bundle_limit",
            )
            descriptor.pop("path", None)
            data = None
        if data is not None:
            used += len(data)
            payloads[descriptor["path"]] = data
        descriptors.append(descriptor)
    return descriptors, payloads


def artifact_candidates(observed):
    root = observed["run_root"]
    result = []
    seen = set()

    def add(relative, scope, kind, media_type, priority, unsafe_path=False):
        if relative in seen or (not unsafe_path and not safe_relative(relative)):
            return
        seen.add(relative)
        result.append(
            {
                "root": root,
                "source": relative,
                "scope": scope,
                "kind": kind,
                "media_type": media_type,
                "priority": priority,
                "unsafe_path": unsafe_path,
            }
        )

    # Only accepted Run-relative payload names are considered.  preparation.json
    # is intentionally excluded: it contains host commands and private topology.
    if observed["coordinator"].resolve() != root.resolve():
        for name in ("assignment.json", "coordinator-request.json"):
            add(name, "run", "json", "application/json", 0)
    if observed.get("preflight"):
        add("preflight-input.json", "preflight", "json", "application/json", 0)
        add("preflight/input.json", "preflight", "json", "application/json", 0)
        add("preflight/output.json", "preflight", "json", "application/json", 0)
        add("preflight/stderr.log", "preflight", "log", "text/plain; charset=utf-8", 1)
        add("preflight/events.jsonl", "preflight", "events", "application/x-ndjson", 2)
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
        for entry in observed["state"]["history"]:
            if entry["outcome"] == "abandoned":
                continue
            base = f"{coordinator_prefix}{entry['directory']}"
            scope = f"component:{entry['sequence']}:{entry['component']}"
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
                safe_name = Path(filename).name == filename and safe_relative(filename)
                add(
                    f"{base}/{filename}" if safe_name else f"{base}/declared-{kind}",
                    scope,
                    artifact_kind,
                    media,
                    2 if kind == "events" else 1,
                    unsafe_path=not safe_name,
                )
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
        return unavailable_descriptor(base, "unsafe_path"), None
    path = candidate["root"] / source
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return unavailable_descriptor(base, "missing"), None
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISREG(facts.st_mode):
        return unavailable_descriptor(base, "unsafe_file"), None
    if facts.st_size == 0:
        return unavailable_descriptor(base, "empty"), None
    if facts.st_size > V2_MAX_ARTIFACT_BYTES:
        return unavailable_descriptor(base, "oversized"), None
    try:
        raw = read_bytes(path, V2_MAX_ARTIFACT_BYTES)
        text = decode_text(raw)
        if candidate["kind"] == "json":
            value = json.loads(text)
            value, changed = sanitize_json_value(value, redactions)
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
                return unavailable_descriptor(base, "empty"), None
            public = ("\n".join(lines) + "\n").encode()
        else:
            sanitized = sanitize_public_text(text, redactions)
            changed = sanitized != text
            public = sanitized.encode()
    except (
        ExportError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return unavailable_descriptor(base, "unsafe_or_invalid"), None
    if len(public) > V2_MAX_ARTIFACT_BYTES:
        return unavailable_descriptor(base, "oversized"), None
    destination = "artifacts/" + source
    descriptor = {
        **base,
        "state": "published",
        "public_bytes": len(public),
        "public_sha256": digest(public),
        "sanitization_status": "sanitized" if changed or public != raw else "unchanged",
        "unavailable_reason": None,
        "path": destination,
    }
    return descriptor, public


def unavailable_descriptor(base, reason):
    return {
        **base,
        "state": "unavailable",
        "public_bytes": 0,
        "public_sha256": None,
        "sanitization_status": "not_applicable",
        "unavailable_reason": reason,
    }


def sanitize_json_value(value, redactions):
    if isinstance(value, str):
        public = sanitize_public_text(value, redactions)
        return public, public != value
    if isinstance(value, list):
        result, changed = [], False
        for item in value:
            public, item_changed = sanitize_json_value(item, redactions)
            result.append(public)
            changed = changed or item_changed
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
    for prefix in sorted(redactions, key=len, reverse=True):
        text = text.replace(prefix, "[redacted-path]")
    text = HOST_PATH.sub("[redacted-path]", text)
    if any(pattern.search(text) for pattern in SENSITIVE_TEXT):
        raise ExportError("included Evidence contains sensitive text")
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


def read_bytes(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        facts = os.fstat(descriptor)
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


def safe_relative(value):
    return (
        isinstance(value, str)
        and value
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode_json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
