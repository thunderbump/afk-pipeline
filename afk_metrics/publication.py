"""Deterministic, bound publication projection for AFK metrics consumers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any

from afk_export import (
    MAX_BUNDLE_FILES,
    MAX_INCLUDED_BYTES,
    MAX_MANIFEST_BYTES,
    V2_MAX_BUNDLE_BYTES,
    ExportError,
    ExportUsageError,
    load_source_v2,
    normalize_run_v2,
    open_directory_beneath,
    read_bytes_at,
    read_bytes_beneath,
    require_directory,
    safe_relative,
)

from .report import build_comparisons, summarize_source

MAX_RUNS = 25
MAX_STAGES = 10_000
MAX_COMPARISONS = 300
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40,64}\Z")
CONTINUATION = re.compile(r"[0-9]+\Z")
RUN_PURPOSES = {
    "acceptance_planning",
    "preparation",
    "publication",
    "run_wall_span",
    "unattributed",
}
OPERATIONAL_PUBLICATION_FIELDS = {"publication", "operational_publication"}

LIMITATIONS = [
    "Metrics do not prove semantic quality or lower code complexity.",
    "Pi cost is a provider/model-rate estimate, not an actual billed charge.",
    "Missing usage, pricing, and deterministic timing are unavailable, never zero.",
]


class PublicationError(ValueError):
    """A publication input or binding failed closed."""


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def load_publication_request(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_absolute():
        raise PublicationError("publication input path must be absolute")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PublicationError("publication input is unavailable") from error
    if len(raw) > MAX_INPUT_BYTES:
        raise PublicationError("publication input exceeds size limit")
    value = _json_object(raw, "publication input")
    runs = value.get("runs")
    if (
        set(value) != {"schema_version", "project", "runs"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("project"), str)
        or not value["project"]
        or not isinstance(runs, list)
        or not 1 <= len(runs) <= MAX_RUNS
    ):
        raise PublicationError("invalid publication input schema")
    for item in runs:
        if not isinstance(item, dict) or set(item) != {"source", "bundle", "selection"}:
            raise PublicationError("invalid publication Run input")
        source, bundle, selection = item["source"], item["bundle"], item["selection"]
        if (
            not isinstance(source, str)
            or not Path(source).is_absolute()
            or not isinstance(bundle, str)
            or not Path(bundle).is_absolute()
            or not isinstance(selection, str)
            or selection not in {"original", "latest"}
            and CONTINUATION.fullmatch(selection) is None
        ):
            raise PublicationError("invalid publication Run input")
    return value


def _inventory_paths(descriptor: int, prefix: str = "") -> set[str]:
    """Inventory a bundle through no-follow descriptors, rejecting odd nodes."""
    paths: set[str] = set()
    for name in os.listdir(descriptor):
        relative = f"{prefix}/{name}" if prefix else name
        facts = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISREG(facts.st_mode):
            paths.add(relative)
        elif stat.S_ISDIR(facts.st_mode):
            paths.add(f"{relative}/")
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            try:
                paths.update(_inventory_paths(child, relative))
            finally:
                os.close(child)
        else:
            raise PublicationError("bundle contains an unsafe filesystem entry")
    return paths


def _read_bundle(bundle: Path) -> tuple[int, dict[str, Any], bytes, str]:
    try:
        require_directory(bundle)
        descriptor = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            manifest_raw = read_bytes_at(
                descriptor, "manifest.json", MAX_MANIFEST_BYTES
            )
            manifest = _json_object(manifest_raw, "bundle manifest")
            schema = manifest.get("schema_version")
            files = manifest.get("files")
            if (
                set(manifest) != {"schema_version", "kind", "identity", "files"}
                or schema not in {2, 3}
                or manifest.get("kind") != "afk-workflow-run"
                or not isinstance(files, list)
                or not 1 <= len(files) <= MAX_BUNDLE_FILES
            ):
                raise PublicationError("bundle manifest identity is invalid")

            payloads: dict[str, bytes] = {}
            total = len(manifest_raw)
            for row in files:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"path", "bytes", "sha256"}
                    or not isinstance(row.get("path"), str)
                    or not safe_relative(row["path"])
                    or row["path"] == "manifest.json"
                    or row["path"] in payloads
                    or not isinstance(row.get("bytes"), int)
                    or isinstance(row.get("bytes"), bool)
                    or row["bytes"] < 0
                    or SHA256.fullmatch(str(row.get("sha256"))) is None
                ):
                    raise PublicationError("bundle manifest file inventory is invalid")
                raw = read_bytes_beneath(descriptor, row["path"], V2_MAX_BUNDLE_BYTES)
                total += len(raw)
                if (
                    row["bytes"] != len(raw)
                    or row["sha256"] != hashlib.sha256(raw).hexdigest()
                ):
                    raise PublicationError("bundle file hash or size disagrees")
                payloads[row["path"]] = raw
            expected_inventory = {"manifest.json", *payloads}
            for path in payloads:
                parts = path.split("/")
                expected_inventory.update(
                    f"{'/'.join(parts[:index])}/" for index in range(1, len(parts))
                )
            if (
                total > V2_MAX_BUNDLE_BYTES
                or _inventory_paths(descriptor) != expected_inventory
            ):
                raise PublicationError("bundle manifest inventory disagrees")
            workflow_raw = payloads.get("workflow-run.json")
            if workflow_raw is None or len(workflow_raw) > MAX_INCLUDED_BYTES:
                raise PublicationError("bundle workflow Run is missing or oversized")
        finally:
            os.close(descriptor)
    except (OSError, ExportError) as error:
        raise PublicationError("bundle is unavailable or unsafe") from error
    workflow = _json_object(workflow_raw, "bundle workflow Run")
    if (
        manifest.get("identity") != workflow.get("identity")
        or workflow.get("schema_version") != schema
    ):
        raise PublicationError("bundle manifest identity is invalid")
    digest = hashlib.sha256(workflow_raw).hexdigest()
    return schema, workflow, workflow_raw, digest


def _publication_evidence_digest(source: Path, observed: dict[str, Any]) -> str | None:
    terminal = Path(observed["terminal_directory"]).resolve()
    relative = terminal.relative_to(source.resolve()).as_posix()
    descriptor = (
        open_directory_beneath(source, relative)
        if relative != "."
        else os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    )
    try:
        try:
            raw = read_bytes_at(descriptor, "publication.json", MAX_INCLUDED_BYTES)
        except FileNotFoundError:
            return None
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


def _semantic_record(record: dict[str, Any], schema: int) -> dict[str, Any]:
    # Bundle-only payload catalogs and operational delivery state do not alter
    # the semantic Run. No identity, lineage, routing, limit, or outcome field
    # is omitted here.
    return {
        key: value
        for key, value in record.items()
        if key
        not in {"artifacts", "inference_sessions", *OPERATIONAL_PUBLICATION_FIELDS}
    } | {"schema_version": schema}


def _empty_metrics() -> dict[str, Any]:
    return {
        "elapsed_seconds": None,
        "elapsed_kind": None,
        "usage": {},
        "compaction_usage": {},
        "usage_coverage": "unavailable",
        "retry_count": 0,
        "cost": {"status": "unavailable", "kind": "unavailable", "amount": None},
        "response_validator_seconds": None,
        "response_validator_coverage": "unavailable",
        "repository_validation_seconds": None,
        "repository_validation_coverage": "unavailable",
    }


def _invocation_metrics(invocation: dict[str, Any]) -> dict[str, Any]:
    result = _empty_metrics()
    metrics = invocation.get("metrics", {})
    elapsed = invocation.get("elapsed", {})
    result.update(
        {
            "elapsed_seconds": elapsed.get("seconds"),
            "elapsed_kind": elapsed.get("kind"),
            "usage": metrics.get("usage", {}),
            "compaction_usage": metrics.get("compaction", {}).get("usage", {}),
            "usage_coverage": metrics.get("coverage", "unavailable"),
            "retry_count": metrics.get("retry_count", 0),
            "cost": metrics.get("cost", result["cost"]),
            "response_validator_seconds": invocation.get("response_validator_seconds"),
            "response_validator_coverage": invocation.get(
                "response_validator_coverage", "unavailable"
            ),
        }
    )
    return result


def _stages(summary: dict[str, Any], project: str, run_id: str) -> list[dict[str, Any]]:
    private = summary["_publication_stage_data"]
    by_component = {}
    run_invocations = []
    for invocation in summary["inference"]["invocations"]:
        owner = invocation.get("_stage_owner")
        if owner and owner["kind"] == "component":
            by_component[owner["sequence"]] = invocation
        elif owner:
            run_invocations.append((owner["purpose"], invocation))
    rows = []
    validation = private["repository_validation"]
    for stage in private["history"]:
        metrics = (
            _invocation_metrics(by_component[stage["sequence"]])
            if stage["sequence"] in by_component
            else _empty_metrics()
        )
        measured = validation.get(
            stage["sequence"], validation.get(str(stage["sequence"]))
        )
        if measured is not None:
            metrics["repository_validation_seconds"] = measured["seconds"]
            metrics["repository_validation_coverage"] = measured["coverage"]
        rows.append(
            {
                "ownership": {
                    "kind": "component",
                    "project": project,
                    "run_id": run_id,
                    "sequence": stage["sequence"],
                    "component": stage["component"],
                },
                "outcome": stage["outcome"],
                **metrics,
            }
        )
    for purpose, invocation in run_invocations:
        if purpose not in RUN_PURPOSES:
            raise PublicationError("unsupported Run-level stage purpose")
        rows.append(
            {
                "ownership": {
                    "kind": "run",
                    "project": project,
                    "run_id": run_id,
                    "purpose": purpose,
                },
                "outcome": invocation.get("outcome"),
                **_invocation_metrics(invocation),
            }
        )
    timing = summary["timing"]
    for purpose, seconds in (
        ("preparation", timing.get("preparation_seconds")),
        ("publication", timing.get("publication_seconds")),
        ("run_wall_span", timing.get("run_wall_span_seconds")),
        ("unattributed", timing.get("unattributed_seconds")),
    ):
        metrics = _empty_metrics()
        metrics["elapsed_seconds"] = seconds
        metrics["elapsed_kind"] = purpose
        rows.append(
            {
                "ownership": {
                    "kind": "run",
                    "project": project,
                    "run_id": run_id,
                    "purpose": purpose,
                },
                "outcome": None,
                **metrics,
            }
        )
    return rows


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(summary))
    value.pop("_publication_stage_data", None)
    for invocation in (value.get("inference") or {}).get("invocations", []):
        invocation.pop("_stage_owner", None)
    return value


def _source_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and REVISION.fullmatch(revision) else None


def build_publication(request: dict[str, Any]) -> dict[str, Any]:
    project = request["project"]
    published = []
    identities = set()
    total_stages = 0
    summaries = []
    for item in request["runs"]:
        source = Path(item["source"])
        bundle = Path(item["bundle"])
        terminal = None if item["selection"] == "latest" else item["selection"]
        try:
            observed = load_source_v2(
                source, project, None, None, terminal_continuation=terminal
            )
            # Retain the complete normalized observation (including evidence
            # hashes) so the metrics read can be bracketed by the exact same
            # authenticated source state rather than merely the Run identity.
            expected, _ = normalize_run_v2(observed, include_artifacts=True)
            publication_digest = _publication_evidence_digest(source, observed)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ExportError,
            ExportUsageError,
        ) as error:
            raise PublicationError("source Run verification failed") from error
        schema, workflow, _raw, workflow_digest = _read_bundle(bundle)
        identity = observed["identity"]
        if (
            identity.get("project") != project
            or workflow.get("identity") != identity
            or _semantic_record(workflow, schema) != _semantic_record(expected, schema)
        ):
            raise PublicationError("source and bundle semantic Run disagree")
        identity_key = (identity.get("project"), identity.get("run_id"))
        if identity_key in identities:
            raise PublicationError("duplicate Run identity")
        identities.add(identity_key)
        if observed.get("state") is None:
            raise PublicationError("selected Run has unsupported metrics shape")
        summary = summarize_source(
            source, observed=observed, include_stage_binding=True
        )
        try:
            confirmed = load_source_v2(
                source, project, None, None, terminal_continuation=terminal
            )
            confirmed_record, _ = normalize_run_v2(confirmed, include_artifacts=True)
            confirmed_publication_digest = _publication_evidence_digest(
                source, confirmed
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ExportError,
            ExportUsageError,
        ) as error:
            raise PublicationError("source Run changed during metrics read") from error
        if (
            confirmed_record != expected
            or confirmed_publication_digest != publication_digest
        ):
            raise PublicationError("source Run changed during metrics read")
        if summary["integrity"]["status"] != "verified":
            raise PublicationError("source metrics verification failed")
        stages = _stages(summary, project, identity["run_id"])
        total_stages += len(stages)
        if total_stages > MAX_STAGES:
            raise PublicationError("publication stage count exceeds limit")
        public_summary = _public_summary(summary)
        summaries.append(public_summary)
        published.append(
            {
                "binding": {
                    "project": project,
                    "run_id": identity["run_id"],
                    "bundle_schema_version": schema,
                    "workflow_run_sha256": workflow_digest,
                },
                "summary": public_summary,
                "stages": stages,
            }
        )
    comparisons = build_comparisons(summaries)
    if len(comparisons) > MAX_COMPARISONS:
        raise PublicationError("publication comparison count exceeds limit")
    return {
        "schema_version": 1,
        "kind": "afk-metrics-publication",
        "project": project,
        "producer": {
            "calculator": "afk_metrics.report",
            "report_schema_version": 1,
            "source_revision": _source_revision(),
        },
        "runs": published,
        "comparisons": comparisons,
        "limitations": LIMITATIONS,
    }


def publish(input_path: Path, destination: Path) -> dict[str, Any]:
    request = load_publication_request(input_path)
    destination = Path(destination)
    if not destination.is_absolute():
        raise PublicationError("publication destination must be absolute")
    if destination.exists() or destination.is_symlink():
        raise PublicationError("publication destination already exists")
    resolved = destination.resolve()
    for item in request["runs"]:
        for name in ("source", "bundle"):
            candidate = Path(item[name]).resolve()
            if (
                resolved == candidate
                or candidate in resolved.parents
                or resolved in candidate.parents
            ):
                raise PublicationError(
                    "publication destination must be separate from inputs"
                )
    publication = build_publication(request)
    raw = (
        json.dumps(publication, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise PublicationError("publication output exceeds size limit")
    # Pin the validated parent before creating the one new file. An ancestor
    # swap therefore cannot redirect caller-owned output into a source Run.
    parent = destination.parent.resolve()
    try:
        expected_parent = require_directory(parent)
        parent_descriptor = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        opened_parent = os.fstat(parent_descriptor)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            expected_parent.st_dev,
            expected_parent.st_ino,
        ):
            raise OSError("destination parent changed")
        temporary_name = None
        try:
            for _attempt in range(10):
                temporary_name = f".{destination.name}.{secrets.token_hex(8)}.tmp"
                try:
                    descriptor = os.open(
                        temporary_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("cannot allocate publication staging file")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o644)
            # Hard-link admission is atomic and refuses a concurrently-created
            # destination; unlike replace(), it preserves the new-file rule.
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except OSError:
                    pass
            os.close(parent_descriptor)
    except (OSError, ExportError) as error:
        raise PublicationError("publication destination cannot be created") from error
    return publication
