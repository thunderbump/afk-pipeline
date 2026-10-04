"""Build bounded synthetic portable bundles for retained-reader regressions.

This writer is test-only. Production consumers authenticate existing records.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from afk_records import source as records

MAX_EVENTS_BYTES = 64 * 1024 * 1024

MAX_BUNDLE_BYTES = 8 * 1024 * 1024

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

INCLUDED_NAMES = {"stdout": "stdout.txt", "stderr": "stderr.txt", "diff": "diff.patch"}


def write_bundle_fixture(
    source_path,
    destination_path,
    project=None,
    run_id=None,
    bead_id=None,
    schema_version=3,
    terminal_continuation=None,
):
    if schema_version not in {1, 2, 3}:
        raise records.ExportUsageError("unsupported Publication Bundle schema")
    source_input = Path(source_path).absolute()
    destination_input = Path(destination_path).absolute()
    source_facts = records.require_directory(source_input)
    if destination_input.exists() or destination_input.is_symlink():
        raise records.ExportError("bundle destination already exists")
    if not destination_input.parent.is_dir():
        raise records.ExportError("bundle destination parent is unavailable")
    source = source_input.resolve()
    destination = destination_input.parent.resolve() / destination_input.name
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise records.ExportError("source and destination must not overlap")
    try:
        source_descriptor = os.open(source, records.DIRECTORY_FLAGS)
    except OSError as error:
        raise records.ExportError("Run source is unavailable") from error
    opened_source_facts = os.fstat(source_descriptor)
    if (opened_source_facts.st_dev, opened_source_facts.st_ino) != (
        source_facts.st_dev,
        source_facts.st_ino,
    ):
        os.close(source_descriptor)
        raise records.ExportError("Run source changed during validation")
    try:
        observed = (
            records.load_source(
                source,
                project,
                run_id,
                bead_id,
                terminal_continuation=terminal_continuation,
                source_descriptor=source_descriptor,
            )
            if schema_version == 1
            else records.load_source_v2(
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
        record, payloads = _legacy_record_fixture(observed)
    elif schema_version == 2:
        record, payloads = records.normalize_run_v2(observed)
    else:
        record, payloads = record_v3_fixture(observed)
    workflow = records.encode_json(record)
    if len(workflow) > records.MAX_INCLUDED_BYTES:
        raise records.ExportError("normalized Run exceeds bundle limits")
    payloads["workflow-run.json"] = workflow
    if len(payloads) > records.MAX_BUNDLE_FILES:
        raise records.ExportError("bundle has too many payload files")
    inventory = [
        {"path": name, "bytes": len(value), "sha256": records.digest(value)}
        for name, value in sorted(payloads.items())
    ]
    manifest = records.encode_json(
        {
            "schema_version": schema_version,
            "kind": "afk-workflow-run",
            "identity": observed["identity"],
            "files": inventory,
        }
    )
    if len(manifest) > records.MAX_MANIFEST_BYTES or len(manifest) + sum(
        map(len, payloads.values())
    ) > (MAX_BUNDLE_BYTES if schema_version == 1 else records.V2_MAX_BUNDLE_BYTES):
        raise records.ExportError("bundle exceeds admission limits")
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


def record_v3_fixture(observed):
    """Create the v3 Run record with one sanitized copy per step object."""
    record, _ = records.normalize_run_v2(observed, include_artifacts=False)
    candidates = records.artifact_candidates(observed)
    descriptors, payloads = records.public_artifacts(
        observed, candidates=artifact_candidates_v3_fixture(observed, candidates)
    )
    record["schema_version"] = 3
    additions = observed.get("continuation_allowances", [])
    if record["response_limit"] + sum(additions) > 2**53 - 1:
        raise records.ExportError(
            "Response allowance exceeds public safe integer range"
        )
    record["continuation_allowances"] = additions
    record["artifacts"] = descriptors
    sessions = _inference_sessions_fixture(candidates)
    if sessions:
        record["inference_sessions"] = sessions
    return (record, payloads)


def _inference_sessions_fixture(candidates):
    """Keep receipt-authenticated status metadata out of the Artifact catalog."""
    terminal_attempts = {}
    for candidate in candidates:
        if candidate["kind"] != "inference_terminal_response_view":
            continue
        value = json.loads(records.decode_text(candidate["generated_raw"]))
        directory = (
            candidate["destination"]
            .removesuffix("/views/terminal-response.json")
            .removeprefix("artifacts/")
        )
        terminal_attempts[candidate["scope"], directory] = value["attempt_number"]
    sessions = []
    for candidate in candidates:
        if candidate["kind"] != "inference_receipt_view":
            continue
        value = json.loads(records.decode_text(candidate["generated_raw"]))
        directory = candidate["source"].removesuffix("/receipt.json")
        terminal_attempt = terminal_attempts.get((candidate["scope"], directory))
        attempts = [
            {**attempt, "terminal": attempt["attempt_number"] == terminal_attempt}
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
        sessions.append(records.sanitize_secret_json_value(session, frozenset())[0])
    return sessions


def artifact_candidates_v3_fixture(observed, originals=None):
    """Select inspectable step objects without v2 private/view descriptor pairs."""
    selected = []
    for original in (
        originals if originals is not None else records.artifact_candidates(observed)
    ):
        candidate = original.copy()
        candidate["secrets_only"] = True
        if (
            candidate["kind"] == "events"
            and candidate["scope"].startswith("component:")
            and candidate["scope"].endswith(":attempt")
        ):
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
            candidate.update(
                private_source=True, inference_view=False, generated_raw=None
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


def _legacy_evidence_fixture(entry, directory, output, redactions):
    artifacts = output.get("artifacts", {})
    if not isinstance(artifacts, dict) or not set(artifacts).issubset(
        records.ARTIFACTS[entry["component"]]
    ):
        raise records.ExportError("component artifacts are invalid")
    descriptors = []
    payloads = {}
    for kind, filename in artifacts.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise records.ExportError("artifact path is invalid")
        path = directory / filename
        limit = MAX_EVENTS_BYTES if kind == "events" else records.MAX_INCLUDED_BYTES
        data = records.read_bytes(path, limit)
        text = records.decode_text(data)
        if kind == "events":
            descriptors.append(
                _evidence_descriptor_fixture(
                    entry, kind, data, text, "omitted", event_counts(text)
                )
            )
        elif data:
            text = records.sanitize_public_text(text, redactions)
            data = text.encode()
            relative = f"evidence/{entry['sequence']:02d}-{entry['component']}/{INCLUDED_NAMES[kind]}"
            descriptors.append(
                _evidence_descriptor_fixture(
                    entry, kind, data, text, "included", path=relative
                )
            )
            payloads[relative] = data
    return (descriptors, payloads)


def _evidence_descriptor_fixture(
    entry, kind, data, text, inclusion, counts=None, path=None
):
    return {
        "sequence": entry["sequence"],
        "component": entry["component"],
        "kind": kind,
        "bytes": len(data),
        "lines": len(text.split("\n")) if data else 0,
        "sha256": records.digest(data),
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


def _legacy_record_fixture(observed):
    record, payloads = records.normalize_run(observed)
    evidence = []
    for entry, normalized in zip(
        observed["state"]["history"], record["history"], strict=True
    ):
        if entry["outcome"] == "abandoned":
            continue
        directory = observed["coordinator"] / entry["directory"]
        items, files = _legacy_evidence_fixture(
            entry,
            directory,
            records.read_json(directory / "output.json"),
            observed["redactions"],
        )
        evidence.extend(items)
        payloads.update(files)
        normalized["evidence"] = [item["kind"] for item in items]
    record["evidence"] = evidence
    return record, payloads
