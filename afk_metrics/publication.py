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
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublicationError("publication input must be a regular file")
        # Reject known-oversized files before allocating for their contents,
        # while retaining a bounded read for synthetic/proc-style regular files
        # whose reported size may not describe their readable bytes.
        if before.st_size > MAX_INPUT_BYTES:
            raise PublicationError("publication input exceeds size limit")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_INPUT_BYTES:
            raise PublicationError("publication input exceeds size limit")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise PublicationError("publication input changed while being read")
    except PublicationError:
        raise
    except OSError as error:
        raise PublicationError("publication input is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
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


def _inventory_matches(descriptor: int, expected: set[str]) -> bool:
    """Compare inventory lazily, without descending into undeclared trees."""
    remaining = set(expected)
    pending = [(os.dup(descriptor), "")]
    visited = 0
    try:
        while pending:
            current, prefix = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        relative = f"{prefix}/{entry.name}" if prefix else entry.name
                        facts = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(facts.st_mode):
                            inventory_path = relative
                            child = None
                        elif stat.S_ISDIR(facts.st_mode):
                            inventory_path = f"{relative}/"
                            child = relative
                        else:
                            raise PublicationError(
                                "bundle contains an unsafe filesystem entry"
                            )
                        visited += 1
                        # At most the declared inventory plus one unexpected
                        # entry is examined. In particular, an undeclared
                        # directory is never recursively inventoried.
                        if visited > len(expected) or inventory_path not in remaining:
                            return False
                        remaining.remove(inventory_path)
                        if child is not None:
                            child_descriptor = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current,
                            )
                            pending.append((child_descriptor, child))
            finally:
                os.close(current)
        return not remaining
    finally:
        for current, _prefix in pending:
            os.close(current)


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

            declared_paths: set[str] = set()
            workflow_raw = None
            total = len(manifest_raw)
            for row in files:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"path", "bytes", "sha256"}
                    or not isinstance(row.get("path"), str)
                    or not safe_relative(row["path"])
                    or row["path"] == "manifest.json"
                    or row["path"] in declared_paths
                    or not isinstance(row.get("bytes"), int)
                    or isinstance(row.get("bytes"), bool)
                    or row["bytes"] < 0
                    or SHA256.fullmatch(str(row.get("sha256"))) is None
                ):
                    raise PublicationError("bundle manifest file inventory is invalid")
                remaining = V2_MAX_BUNDLE_BYTES - total
                if row["bytes"] > remaining:
                    raise PublicationError("bundle exceeds admission limits")
                if (
                    row["path"] == "workflow-run.json"
                    and row["bytes"] > MAX_INCLUDED_BYTES
                ):
                    raise PublicationError(
                        "bundle workflow Run is missing or oversized"
                    )
                # Bound this read by both the declared size and the remaining
                # aggregate budget. A lying or oversized entry is rejected by
                # fstat before its contents can be accumulated.
                raw = read_bytes_beneath(descriptor, row["path"], row["bytes"])
                if (
                    row["bytes"] != len(raw)
                    or row["sha256"] != hashlib.sha256(raw).hexdigest()
                ):
                    raise PublicationError("bundle file hash or size disagrees")
                total += len(raw)
                declared_paths.add(row["path"])
                if row["path"] == "workflow-run.json":
                    workflow_raw = raw
            expected_inventory = {"manifest.json", *declared_paths}
            for path in declared_paths:
                parts = path.split("/")
                expected_inventory.update(
                    f"{'/'.join(parts[:index])}/" for index in range(1, len(parts))
                )
            if not _inventory_matches(descriptor, expected_inventory):
                raise PublicationError("bundle manifest inventory disagrees")
            if workflow_raw is None:
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
            # The bundle comparison below uses only the artifact-free semantic
            # Run. Separately retain Export's authenticated artifact and
            # inference-session projection to bracket every evidence source
            # consumed by the metrics report (including receipts and events).
            expected, _ = normalize_run_v2(observed, include_artifacts=False)
            evidence_checkpoint, _ = normalize_run_v2(observed, include_artifacts=True)
            publication_digest = _publication_evidence_digest(source, observed)
            # The report must consume the identities captured by this exact
            # normalization pass, rather than merely agreeing with another pass
            # after a transient replacement has been restored.
            observed["_metrics_publication_sha256"] = publication_digest
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
            confirmed_record, _ = normalize_run_v2(confirmed, include_artifacts=False)
            confirmed_evidence_checkpoint, _ = normalize_run_v2(
                confirmed, include_artifacts=True
            )
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
            or confirmed_evidence_checkpoint != evidence_checkpoint
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
    # Pin the already-separated parent before reading and calculating metrics.
    # A symlinked ancestor can otherwise be redirected to an input while the
    # publication is being built and make a later open mutate that input.
    parent = resolved.parent
    parent_descriptor = None
    temporary_name = None
    try:
        expected_parent = require_directory(parent)
        parent_descriptor = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        opened_parent = os.fstat(parent_descriptor)
        parent_identity = (opened_parent.st_dev, opened_parent.st_ino)
        opened_path = Path(f"/proc/self/fd/{parent_descriptor}").resolve(strict=True)
        if (
            parent_identity != (expected_parent.st_dev, expected_parent.st_ino)
            or opened_path != parent
        ):
            raise OSError("destination parent changed")

        publication = build_publication(request)
        raw = (
            json.dumps(publication, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if len(raw) > MAX_OUTPUT_BYTES:
            raise PublicationError("publication output exceeds size limit")

        # Reject an alias changed during the build as well as keeping all file
        # creation relative to the descriptor. A subsequent swap cannot
        # redirect descriptor-relative creation into the replacement target.
        current_parent = os.stat(destination.parent)
        if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
            raise OSError("destination parent changed")
        for _attempt in range(10):
            temporary_name = f".{resolved.name}.{secrets.token_hex(8)}.tmp"
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
            resolved.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except (OSError, ExportError) as error:
        raise PublicationError("publication destination cannot be created") from error
    finally:
        if parent_descriptor is not None:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except OSError:
                    pass
            os.close(parent_descriptor)
    return publication
