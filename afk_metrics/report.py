"""Read-only metrics projection over authenticated AFK evidence.

Only metadata, timings, counts, and usage fields are retained.  Event streams are
processed one line at a time and event content is never copied into the report.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from afk_export import (
    ExportError,
    ExportUsageError,
    load_source,
    receipt_bound_inference_artifacts,
)

TOKEN_FIELDS = (
    "input",
    "output",
    "cacheRead",
    "cacheWrite",
    "totalTokens",
    "reasoning",
)


def _number(value: Any) -> float | int | None:
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def _usage(value: Any) -> dict[str, float | int]:
    if not isinstance(value, dict):
        return {}
    return {
        name: number
        for name in TOKEN_FIELDS
        if (number := _number(value.get(name))) is not None
    }


def _add(target: dict[str, float | int], value: dict[str, float | int]) -> None:
    for name, number in value.items():
        target[name] = target.get(name, 0) + number


def _message_usage(event: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None, None
    return message, message.get("usage", event.get("usage"))


def parse_pi_events(path: Path) -> dict[str, Any]:
    """Stream a Pi JSONL file and project finalized usage without event payloads."""
    usage: dict[str, float | int] = {}
    compact_usage: dict[str, float | int] = {}
    compact_cost = 0.0
    compaction_has_cost = False
    cost = 0.0
    has_cost = False
    cost_measurements = 0
    finalized = 0
    missing = 0
    retries = 0
    compactions = 0
    seen_messages: set[str] = set()
    seen_compactions: set[str] = set()
    identities: set[tuple[str | None, str | None]] = set()
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as error:
                raise ValueError(
                    f"invalid JSONL event at line {line_number}"
                ) from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise TypeError(f"invalid JSONL event shape at line {line_number}")
            kind = event["type"]
            if kind in {"auto_retry_start", "auto_retry_end"}:
                # start is preferred; old streams may have only end. Stable keys
                # prevent counting both copies of one provider retry.
                attempt = event.get("attempt")
                key = f"{attempt}" if attempt is not None else f"line:{line_number}"
                # Keep retries separate from message identities with a prefix.
                retry_key = "retry:" + key
                if retry_key not in seen_messages:
                    seen_messages.add(retry_key)
                    retries += 1
            if kind == "message_end":
                message, raw_usage = _message_usage(event)
                if message is None:
                    continue
                identity = message.get("id")
                key = (
                    f"message:{identity}"
                    if isinstance(identity, str) and identity
                    else f"event:{line_number}"
                )
                if key in seen_messages:
                    continue
                seen_messages.add(key)
                finalized += 1
                provider = message.get("provider")
                model = message.get("model")
                if isinstance(provider, str) or isinstance(model, str):
                    identities.add(
                        (
                            provider if isinstance(provider, str) else None,
                            model if isinstance(model, str) else None,
                        )
                    )
                measured = _usage(raw_usage)
                if not measured:
                    missing += 1
                    continue
                _add(usage, measured)
                if isinstance(raw_usage, dict):
                    raw_cost = raw_usage.get("cost")
                    amount = (
                        _number(raw_cost.get("total"))
                        if isinstance(raw_cost, dict)
                        else _number(raw_cost)
                    )
                    if amount is not None:
                        cost += float(amount)
                        has_cost = True
                        cost_measurements += 1
            elif kind == "compaction_end":
                result = event.get("result")
                raw_usage = result.get("usage") if isinstance(result, dict) else None
                measured = _usage(raw_usage)
                if not measured:
                    continue
                identity = event.get("id")
                # No upstream id is guaranteed. Distinct un-identified events
                # may be equal aggregates, so only an upstream id deduplicates.
                key = str(identity) if identity is not None else f"event:{line_number}"
                if key in seen_compactions:
                    continue
                seen_compactions.add(key)
                compactions += 1
                _add(compact_usage, measured)
                if isinstance(raw_usage, dict):
                    raw_cost = raw_usage.get("cost")
                    amount = (
                        _number(raw_cost.get("total"))
                        if isinstance(raw_cost, dict)
                        else _number(raw_cost)
                    )
                    if amount is not None:
                        compact_cost += float(amount)
                        compaction_has_cost = True
                        cost_measurements += 1
    partial = missing > 0 or retries > 0
    coverage = (
        "partial"
        if partial
        else ("complete" if finalized or compactions else "unavailable")
    )
    cost_status = (
        "unavailable"
        if not has_cost and not compaction_has_cost
        else "partial"
        if cost_measurements < finalized + compactions
        else "reported_estimate"
    )
    return {
        "finalized_requests": finalized,
        "request_count_exact": compactions == 0 and not partial,
        "retry_count": retries,
        "coverage": coverage,
        "usage": usage,
        "compaction": {"aggregate_count": compactions, "usage": compact_usage},
        "cost": {
            "status": cost_status,
            "kind": "pi_reported_estimate" if has_cost else "unavailable",
            "amount": cost if has_cost else None,
            "currency": None,
            "billed_charge": False if has_cost else None,
            "provenance": {
                "calculator": "Pi model rates" if has_cost else None,
                "pi_version": None,
                "price_table_date": None,
            },
        },
        "compaction_cost": compact_cost if compaction_has_cost else None,
        "cost_measurements": cost_measurements,
        "identities": [
            {"provider": provider, "model": model}
            for provider, model in sorted(
                identities, key=lambda item: (item[0] or "", item[1] or "")
            )
        ],
    }


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _seconds(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return max(
            0.0,
            (
                datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))
            ).total_seconds(),
        )
    except ValueError:
        return None


def _safe_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def _sum_usage(
    items: list[dict[str, Any]], branch: str = "usage"
) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    for item in items:
        value = item.get(branch, {})
        if branch == "compaction":
            value = value.get("usage", {})
        _add(result, value)
    return result


def _hash_regular_beneath(directory: Path, relative: str) -> str:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("unsafe receipt artifact identity")
    path = directory / candidate
    if (
        path.is_symlink()
        or not path.is_file()
        or directory.resolve() not in path.resolve().parents
    ):
        raise ValueError("receipt artifact is unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_generic_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    """Verify common runtime hash bindings for an unsupported adapter.

    Export's closed Pi verifier cannot accept optional adapters by design. This
    generic boundary authenticates the same retained files but never interprets
    their event protocol or invents usage support.
    """
    hashes = receipt.get("hashes")
    if not isinstance(hashes, dict) or not isinstance(
        hashes.get("invocation_sha256"), str
    ):
        raise TypeError("receipt omits invocation identity")
    top_level = {
        "invocation_sha256": "invocation.json",
        "prompt_sha256": "prompt.json",
        "task_prompt_sha256": "task-prompt.txt",
        "adapter_contract_sha256": "adapter-contract.json",
    }
    for field, relative in top_level.items():
        expected = hashes.get(field)
        if expected is not None and (
            not isinstance(expected, str)
            or _hash_regular_beneath(directory, relative) != expected
        ):
            raise ValueError("receipt artifact hash disagrees")
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list):
        raise TypeError("receipt attempts are invalid")
    for attempt in attempts:
        artifacts = attempt.get("artifacts") if isinstance(attempt, dict) else None
        if not isinstance(artifacts, dict):
            raise TypeError("receipt attempt artifacts are invalid")
        for name in ("events", "stderr", "response"):
            relative = artifacts.get(name)
            expected = artifacts.get(name + "_sha256")
            if relative is not None and (
                not isinstance(relative, str)
                or not isinstance(expected, str)
                or _hash_regular_beneath(directory, relative) != expected
            ):
                raise ValueError("receipt attempt artifact hash disagrees")


def _invocation(root: Path, relative: str, purpose: str) -> dict[str, Any]:
    receipt_path = root / relative / "receipt.json"
    receipt = _safe_json(receipt_path)
    identity = (
        receipt.get("identity") if isinstance(receipt.get("identity"), dict) else {}
    )
    family = identity.get("adapter_family")
    timing = receipt.get("timing") if isinstance(receipt.get("timing"), dict) else {}
    base = {
        "source_event_identity": receipt.get("hashes", {}).get("invocation_sha256")
        if isinstance(receipt.get("hashes"), dict)
        else None,
        "purpose": purpose,
        "adapter": identity.get("adapter"),
        "adapter_family": family,
        "provider": identity.get("provider"),
        "model": identity.get("model"),
        "outcome": receipt.get("outcome"),
        "attempt_count": receipt.get("attempt_count"),
        "elapsed": {
            "kind": "invocation_adapter_elapsed_not_pure_inference",
            "seconds": _number(timing.get("duration_seconds")),
            "started_at": timing.get("started_at")
            if isinstance(timing.get("started_at"), str)
            else None,
            "ended_at": timing.get("ended_at")
            if isinstance(timing.get("ended_at"), str)
            else None,
        },
        "response_validator_seconds": None,
    }
    attempts = (
        receipt.get("attempts") if isinstance(receipt.get("attempts"), list) else []
    )
    validator = sum(
        float(value)
        for attempt in attempts
        if isinstance(attempt, dict)
        and isinstance(attempt.get("validation"), dict)
        and (value := _number(attempt["validation"].get("validator_duration_seconds")))
        is not None
    )
    base["response_validator_seconds"] = validator if validator else None
    if family != "pi":
        _verify_generic_receipt(root / relative, receipt)
        return {
            **base,
            "metrics": {
                "coverage": "unavailable",
                "reason": "unsupported_adapter",
                "usage": {},
                "compaction": {"aggregate_count": 0, "usage": {}},
                "retry_count": 0,
                "cost": {
                    "status": "unavailable",
                    "kind": "unavailable",
                    "amount": None,
                },
            },
        }
    # Reuse Export's receipt boundary: this authenticates invocation identity and
    # every declared event hash with bounded metadata and streaming file hashes.
    receipt_bound_inference_artifacts(root, relative, purpose, None)
    parsed = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        artifacts = attempt.get("artifacts")
        event_name = artifacts.get("events") if isinstance(artifacts, dict) else None
        if isinstance(event_name, str):
            parsed.append(parse_pi_events(root / relative / event_name))
    if any(item["coverage"] == "partial" for item in parsed) or len(parsed) < len(
        attempts
    ):
        merged_coverage = "partial"
    elif parsed and all(item["coverage"] == "complete" for item in parsed):
        merged_coverage = "complete"
    elif receipt.get("outcome") not in {"succeeded", None}:
        # A failed/retried/aborted request can legitimately have no final usage.
        merged_coverage = "partial"
    else:
        merged_coverage = "unavailable"
    merged = {
        "finalized_requests": sum(item["finalized_requests"] for item in parsed),
        "request_count_exact": merged_coverage == "complete"
        and all(item["request_count_exact"] for item in parsed),
        "retry_count": sum(item["retry_count"] for item in parsed),
        "coverage": merged_coverage,
        "usage": _sum_usage(parsed),
        "compaction": {
            "aggregate_count": sum(
                item["compaction"]["aggregate_count"] for item in parsed
            ),
            "usage": _sum_usage(parsed, "compaction"),
        },
    }
    costs = [
        (item["cost"]["amount"] or 0) + (item["compaction_cost"] or 0)
        for item in parsed
        if item["cost"]["amount"] is not None or item["compaction_cost"] is not None
    ]
    measured_groups = sum(
        item["finalized_requests"] + item["compaction"]["aggregate_count"]
        for item in parsed
    )
    cost_groups = sum(item["cost_measurements"] for item in parsed)
    cost_status = (
        "unavailable"
        if not costs
        else "partial"
        if cost_groups < measured_groups
        else "reported_estimate"
    )
    merged["cost"] = {
        "status": cost_status,
        "kind": "pi_reported_estimate" if costs else "unavailable",
        "amount": sum(costs) if costs else None,
        "currency": None,
        "billed_charge": False if costs else None,
        "provenance": {
            "calculator": "Pi model rates" if costs else None,
            "pi_version": None,
            "price_table_date": None,
        },
    }
    observed_identities = {
        (identity["provider"], identity["model"])
        for item in parsed
        for identity in item["identities"]
    }
    if len(observed_identities) == 1:
        provider, event_model = next(iter(observed_identities))
        base["provider"] = provider
        if base["model"] is None:
            base["model"] = event_model
    base["observed_identities"] = [
        {"provider": provider, "model": model}
        for provider, model in sorted(
            observed_identities, key=lambda item: (item[0] or "", item[1] or "")
        )
    ]
    return {**base, "metrics": merged}


def summarize_source(source: Path) -> dict[str, Any]:
    source = Path(source)
    try:
        observed = load_source(source, None, None, None)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        ExportError,
        ExportUsageError,
    ) as error:
        return {
            "source_identity": hashlib.sha256(
                str(source.resolve()).encode()
            ).hexdigest(),
            "integrity": {"status": "invalid", "error": type(error).__name__},
            "run_identity": None,
            "work": None,
            "outcome": None,
            "inference": None,
            "timing": None,
        }
    identity = observed["identity"]
    assignment = observed["assignment"]
    state = observed["state"]
    root = source.resolve()
    coordinator_prefix = (
        "" if observed["coordinator"].resolve() == root else "coordinator/"
    )
    candidates: list[tuple[str, str]] = []
    if (root / "planner/inference").exists():
        candidates.append(("planner/inference", "acceptance_planning"))
    for entry in state["history"]:
        if entry.get("outcome") == "abandoned":
            continue
        relative = f"{coordinator_prefix}{entry['directory']}/inference"
        if (root / relative).exists() or (root / relative).is_symlink():
            purpose = {
                "assessment": "finding_assessment",
                "response": "feedback_response",
            }.get(entry["component"], entry["component"])
            candidates.append((relative, purpose))
    invocations = []
    seen = set()
    try:
        for relative, purpose in candidates:
            item = _invocation(root, relative, purpose)
            key = item["source_event_identity"] or _canonical_hash([relative, purpose])
            if key not in seen:
                seen.add(key)
                invocations.append(item)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ExportError) as error:
        return {
            "source_identity": _canonical_hash([identity, assignment]),
            "integrity": {"status": "invalid", "error": type(error).__name__},
            "run_identity": identity,
            "work": None,
            "outcome": None,
            "inference": None,
            "timing": None,
        }
    validation_durations = []
    validation_results = []
    for entry in state["history"]:
        if (
            entry.get("component") == "validation"
            and entry.get("outcome") != "abandoned"
        ):
            output = _safe_json(
                root / coordinator_prefix / entry["directory"] / "output.json"
            )
            duration = _number(output.get("duration_seconds"))
            if duration is not None:
                validation_durations.append(duration)
            validation_results.append(output.get("outcome"))
    receipt_metrics = [item["metrics"] for item in invocations]
    elapsed_values = [item["elapsed"]["seconds"] for item in invocations]
    invocation_seconds = (
        sum(elapsed_values)
        if all(value is not None for value in elapsed_values)
        else None
    )
    prep = observed.get("preparation")
    timestamps = prep.get("timestamps", {}) if isinstance(prep, dict) else {}
    run_span = _seconds(timestamps.get("started_at"), timestamps.get("finished_at"))
    prep_seconds = _seconds(timestamps.get("started_at"), timestamps.get("prepared_at"))
    publication_seconds = None
    completion_acceptance = "unavailable"
    integration_status = "unavailable"
    publication_path = observed["terminal_directory"] / "publication.json"
    if publication_path.is_file() and not publication_path.is_symlink():
        try:
            publication = _safe_json(publication_path)
            if publication.get("schema_version") == 1:
                publication_seconds = _seconds(
                    publication.get("started_at"), publication.get("finished_at")
                )
                if isinstance(publication.get("admission_outcome"), str):
                    completion_acceptance = publication["admission_outcome"]
                if publication.get("status") in {"succeeded", "failed"}:
                    integration_status = publication["status"]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # Publication is optional post-Run evidence. Its report measurement
            # can be unavailable without changing trust in the sealed Run.
            publication_seconds = None
    known_nonoverlap = (
        (prep_seconds or 0) + (invocation_seconds or 0) + sum(validation_durations)
    )
    active_intervals = []
    if prep_seconds is not None:
        active_intervals.append({"kind": "preparation", "seconds": prep_seconds})
    active_intervals.extend(
        {"kind": "inference_invocation", "seconds": value}
        for value in elapsed_values
        if value is not None
    )
    active_intervals.extend(
        {"kind": "repository_validation", "seconds": value}
        for value in validation_durations
    )
    source_identity = _canonical_hash({"identity": identity, "assignment": assignment})
    retries = sum(item.get("retry_count", 0) for item in receipt_metrics)
    available_costs = [
        metrics["cost"]["amount"]
        for metrics in receipt_metrics
        if metrics.get("cost", {}).get("amount") is not None
    ]
    total_cost_status = (
        "unavailable"
        if not available_costs
        else "partial"
        if any(
            metrics.get("cost", {}).get("status") != "reported_estimate"
            for metrics in receipt_metrics
        )
        else "reported_estimate"
    )
    return {
        "source_identity": source_identity,
        "integrity": {"status": "verified"},
        "run_identity": {**identity, "bead_id": observed.get("bead_id")},
        "work": {
            "objective_sha256": hashlib.sha256(
                assignment["objective"].encode()
            ).hexdigest(),
            "base_commit": prep.get("repository", {}).get("base_commit")
            if isinstance(prep, dict)
            else None,
            "validation_conditions_sha256": _canonical_hash(
                observed["request"].get("validation")
            ),
        },
        "outcome": {
            "terminal": observed["output"].get("outcome"),
            "coordinator_status": state.get("status"),
            "validation_results": validation_results,
            "repair_count": sum(
                1 for entry in state["history"] if entry.get("component") == "response"
            ),
            "retry_count": retries,
            "completion_acceptance": completion_acceptance,
            "integration_status": integration_status,
        },
        "inference": {
            "invocations": invocations,
            "totals": {
                "elapsed_seconds": invocation_seconds,
                "usage": _sum_usage(receipt_metrics),
                "compaction_usage": _sum_usage(receipt_metrics, "compaction"),
                "cost": {
                    "status": total_cost_status,
                    "kind": "pi_reported_estimate"
                    if available_costs
                    else "unavailable",
                    "amount": sum(available_costs) if available_costs else None,
                    "currency": None,
                    "billed_charge": False if available_costs else None,
                },
            },
        },
        "timing": {
            "run_wall_span_seconds": run_span,
            "active_execution_intervals": active_intervals,
            "preparation_seconds": prep_seconds,
            "publication_seconds": publication_seconds,
            "inference_invocation_seconds": invocation_seconds,
            "repository_validation_seconds": sum(validation_durations)
            if validation_durations
            else None,
            "response_validator_seconds": sum(
                item["response_validator_seconds"] or 0 for item in invocations
            )
            or None,
            "deterministic_steps": {
                "Validation": sum(validation_durations)
                if validation_durations
                else "unavailable",
                "Change": "unavailable",
                "Iteration": "unavailable",
            },
            "continuation_wait_gaps": "unavailable",
            "unattributed_seconds": (
                max(0, run_span - known_nonoverlap)
                if run_span is not None and known_nonoverlap <= run_span
                else None
            ),
            "overlap_note": "unattributed excludes only known non-overlapping preparation, invocation and Validation intervals; nested response validation is not subtracted again",
        },
    }


def build_report(sources: list[Path]) -> dict[str, Any]:
    runs = []
    seen = set()
    for source in sources:
        run = summarize_source(source)
        if run["source_identity"] not in seen:
            seen.add(run["source_identity"])
            runs.append(run)
    comparisons = []
    for left_index, left in enumerate(runs):
        for right in runs[left_index + 1 :]:
            if left["work"] is None or right["work"] is None:
                warnings = ["integrity prevents comparison"]
            else:
                names = {
                    "objective_sha256": "objective",
                    "base_commit": "base code state",
                    "validation_conditions_sha256": "validation conditions",
                }
                warnings = [
                    f"different {label}"
                    for field, label in names.items()
                    if left["work"].get(field) != right["work"].get(field)
                ]
            comparisons.append(
                {
                    "left": left["source_identity"],
                    "right": right["source_identity"],
                    "equivalent_frozen_conditions": not warnings,
                    "warnings": warnings,
                    "ranking": "not_provided" if warnings else "observational_only",
                }
            )
    return {
        "schema_version": 1,
        "report_kind": "afk_retained_run_metrics",
        "runs": runs,
        "comparisons": comparisons,
        "limitations": [
            "Metrics do not prove semantic quality or lower code complexity.",
            "Pi cost is a provider/model-rate estimate, not an actual billed charge.",
            "Missing usage, pricing, and deterministic timing are unavailable, never zero.",
        ],
    }
