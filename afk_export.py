"""Deterministically export one sealed AFK Run as a portable bundle."""

import hashlib
import json
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


def export_run(source_path, destination_path, project=None, run_id=None, bead_id=None):
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
    observed = load_source(source, project, run_id, bead_id)
    record, payloads = normalize_run(observed)
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
            "schema_version": 1,
            "kind": "afk-workflow-run",
            "identity": observed["identity"],
            "files": inventory,
        }
    )
    if (
        len(manifest) > MAX_MANIFEST_BYTES
        or len(manifest) + sum(map(len, payloads.values())) > MAX_BUNDLE_BYTES
    ):
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
        "schema_version": 1,
        "outcome": "exported",
        "identity": observed["identity"],
        "destination": str(destination),
    }


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


def normalize_run(observed):
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
