"""Read-only metrics projection over authenticated AFK evidence.

Only metadata, timings, counts, and usage fields are retained.  Event streams are
processed one line at a time and event content is never copied into the report.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from afk_coordinate.contract import validate_component_output
from afk_export import (
    ExportError,
    ExportUsageError,
    load_source,
    normalize_component_output,
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

# A stream is bounded both across records and within one record. Pi events are
# metadata-rich but should never approach this limit; rejecting an oversized
# record prevents a corrupt artifact from materializing an unbounded line.
MAX_JSONL_RECORD_BYTES = 1024 * 1024

# Identity labels are the only event strings emitted by this projection. Keep
# them deliberately narrower than arbitrary Pi strings so an event cannot use a
# nominal model/provider field as a prompt, tool-output, or credential channel.
SAFE_IDENTITY_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}\Z")
SENSITIVE_IDENTITY_PREFIX = re.compile(
    r"(?:sk-|api[_-]?key|bearer|token|secret|password)", re.IGNORECASE
)


def _identity_label(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTITY_LABEL.fullmatch(value)
        or SENSITIVE_IDENTITY_PREFIX.match(value)
    ):
        return None
    return value


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


def _reported_cost(raw_usage: Any) -> float | int | None:
    if not isinstance(raw_usage, dict):
        return None
    raw_cost = raw_usage.get("cost")
    return (
        _number(raw_cost.get("total"))
        if isinstance(raw_cost, dict)
        else _number(raw_cost)
    )


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
    with Path(path).open("rb") as stream:
        line_number = 0
        while line := stream.readline(MAX_JSONL_RECORD_BYTES + 1):
            line_number += 1
            if len(line) > MAX_JSONL_RECORD_BYTES:
                raise ValueError(f"oversized JSONL event at line {line_number}")
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
                provider = _identity_label(message.get("provider"))
                model = _identity_label(message.get("model"))
                if provider is not None or model is not None:
                    identities.add((provider, model))
                measured = _usage(raw_usage)
                amount = _reported_cost(raw_usage)
                if amount is not None:
                    cost += float(amount)
                    has_cost = True
                    cost_measurements += 1
                if not measured:
                    # Cost is independent evidence: retain it while correctly
                    # marking token coverage incomplete.
                    missing += 1
                    continue
                _add(usage, measured)
            elif kind == "compaction_end":
                result = event.get("result")
                raw_usage = result.get("usage") if isinstance(result, dict) else None
                measured = _usage(raw_usage)
                amount = _reported_cost(raw_usage)
                if not measured and amount is None:
                    continue
                identity = event.get("id")
                # No upstream id is guaranteed. Distinct un-identified events
                # may be equal aggregates, so only an upstream id deduplicates.
                key = str(identity) if identity is not None else f"event:{line_number}"
                if key in seen_compactions:
                    continue
                seen_compactions.add(key)
                compactions += 1
                if measured:
                    _add(compact_usage, measured)
                else:
                    missing += 1
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
    """Authenticate the complete current runtime contract without parsing usage."""
    invocation = _safe_json(directory / "invocation.json")
    prompt = _safe_json(directory / "prompt.json")
    hashes = receipt.get("hashes")
    identity = receipt.get("identity")
    policy = receipt.get("policy")
    timing = receipt.get("timing")
    adapter = invocation.get("adapter")
    if (
        set(receipt)
        != {
            "schema_version",
            "identity",
            "hashes",
            "policy",
            "timing",
            "attempt_count",
            "attempts",
            "protocol",
            "validation",
            "terminal_response",
            "outcome",
        }
        or set(invocation)
        != {
            "schema_version",
            "purpose",
            "task_contract_version",
            "prompt",
            "requested_capability",
            "execution_root",
            "evidence_directory",
            "timeout_seconds",
            "adapter",
        }
        or receipt.get("schema_version") != 1
        or invocation.get("schema_version") != 1
        or not isinstance(hashes, dict)
        or not isinstance(identity, dict)
        or set(identity) != {"runtime", "adapter"}
        or identity.get("runtime") != "afk-inference-v1"
        or not isinstance(policy, dict)
        or set(policy)
        != {
            "requested_capability",
            "system_instructions",
            "max_attempts",
            "single_deadline",
            "validator_trust",
        }
        or not isinstance(timing, dict)
        or set(timing)
        != {"started_at", "ended_at", "timeout_seconds", "duration_seconds"}
        or not isinstance(adapter, dict)
        or set(adapter) != {"kind", "identity", "capabilities"}
        or adapter.get("kind") != "fixture"
        or identity.get("adapter") != adapter.get("identity")
        or invocation.get("prompt") != prompt
        or set(prompt)
        != {
            "system",
            "purpose",
            "task_contract_version",
            "trusted_task_instructions",
            "untrusted_task_data",
        }
        or prompt.get("purpose") != invocation.get("purpose")
        or prompt.get("task_contract_version")
        != invocation.get("task_contract_version")
        or policy.get("requested_capability") != invocation.get("requested_capability")
        or policy.get("system_instructions") != prompt.get("system")
        or timing.get("timeout_seconds") != invocation.get("timeout_seconds")
    ):
        raise ValueError("generic receipt identity or policy disagrees")
    required_hashes = {
        "invocation_sha256": "invocation.json",
        "prompt_sha256": "prompt.json",
        "adapter_script_sha256": "fixture-script.json",
    }
    if set(hashes) != set(required_hashes):
        raise ValueError("generic receipt hash catalog is invalid")
    for field, relative in required_hashes.items():
        expected = hashes.get(field)
        if (
            not isinstance(expected, str)
            or _hash_regular_beneath(directory, relative) != expected
        ):
            raise ValueError("receipt artifact hash disagrees")
    script = json.loads((directory / "fixture-script.json").read_text())
    capabilities = adapter.get("capabilities")
    if (
        not isinstance(script, list)
        or not script
        or not isinstance(capabilities, list)
        or not all(isinstance(item, str) for item in capabilities)
        or policy.get("max_attempts") != len(script)
        or policy.get("single_deadline") is not True
        or policy.get("validator_trust") != "trusted_in_process"
    ):
        raise ValueError("generic adapter contract disagrees")
    duration = _number(timing.get("duration_seconds"))
    timeout = _number(timing.get("timeout_seconds"))
    if (
        duration is None
        or timeout is None
        or _seconds(timing.get("started_at"), timing.get("ended_at")) is None
    ):
        raise ValueError("generic receipt timing is invalid")
    attempts = receipt.get("attempts")
    if (
        not isinstance(attempts, list)
        or receipt.get("attempt_count") != len(attempts)
        or len(attempts) > len(script)
    ):
        raise TypeError("receipt attempts are invalid")
    for index, attempt in enumerate(attempts, 1):
        artifacts = attempt.get("artifacts") if isinstance(attempt, dict) else None
        if (
            not {"attempt_number", "duration_seconds", "protocol", "artifacts"}
            <= set(attempt)
            <= {
                "attempt_number",
                "duration_seconds",
                "protocol",
                "artifacts",
                "process",
                "validation",
            }
            or attempt.get("attempt_number") != index
            or _number(attempt.get("duration_seconds")) is None
            or not isinstance(attempt.get("protocol"), dict)
            or not isinstance(artifacts, dict)
            or set(artifacts)
            != {
                "events",
                "events_sha256",
                "stderr",
                "stderr_sha256",
                "response",
                "response_sha256",
            }
        ):
            raise TypeError("receipt attempt contract is invalid")
        for name, suffix in (
            ("events", "events.jsonl"),
            ("stderr", "stderr.log"),
            ("response", "response.json"),
        ):
            relative = artifacts[name]
            expected = artifacts[name + "_sha256"]
            expected_relative = f"attempts/{index}/{suffix}"
            if relative is None and expected is None and name == "response":
                continue
            if (
                relative != expected_relative
                or not isinstance(expected, str)
                or _hash_regular_beneath(directory, relative) != expected
            ):
                raise ValueError("receipt attempt artifact hash disagrees")
    expected_protocol = (
        attempts[-1]["protocol"] if attempts else {"status": "not_started"}
    )
    if receipt.get("protocol") != expected_protocol:
        raise ValueError("receipt terminal protocol disagrees")
    validation = receipt.get("validation")
    if not isinstance(validation, dict) or not isinstance(
        validation.get("status"), str
    ):
        raise TypeError("receipt validation is invalid")
    attempt_number = validation.get("attempt_number")
    if attempt_number is not None:
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or not 1 <= attempt_number <= len(attempts)
            or attempts[attempt_number - 1].get("validation") != validation
        ):
            raise ValueError("receipt validation identity disagrees")
        response_path = attempts[attempt_number - 1]["artifacts"].get("response")
        if response_path is None or json.loads(
            (directory / response_path).read_text()
        ) != receipt.get("terminal_response"):
            raise ValueError("receipt terminal response disagrees")
    elif receipt.get("terminal_response") is not None:
        raise ValueError("receipt terminal response lacks identity")
    outcome = receipt.get("outcome")
    validation_status = validation["status"]
    if outcome not in {
        "succeeded",
        "response_rejected",
        "adapter_failed",
        "validator_failed",
        "timed_out",
        "interrupted",
    } or (
        outcome in {"succeeded", "response_rejected", "validator_failed"}
        and validation_status
        != {
            "succeeded": "accepted",
            "response_rejected": "response_rejected",
            "validator_failed": "validator_failed",
        }[outcome]
    ):
        raise ValueError("receipt outcome is invalid")


def _invocation(root: Path, relative: str, purpose: str) -> dict[str, Any]:
    receipt_path = root / relative / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("inference receipt is unavailable")
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
        "adapter": _identity_label(identity.get("adapter")),
        "adapter_family": _identity_label(family),
        "provider": _identity_label(identity.get("provider")),
        "model": _identity_label(identity.get("model")),
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


def _invalid_source(
    source: Path, error: BaseException, identity: Any = None, assignment: Any = None
) -> dict[str, Any]:
    stable = (
        _canonical_hash({"identity": identity, "assignment": assignment})
        if identity is not None and assignment is not None
        else hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    )
    return {
        "source_identity": stable,
        "integrity": {"status": "invalid", "error": type(error).__name__},
        "run_identity": identity,
        "work": None,
        "outcome": None,
        "inference": None,
        "timing": None,
    }


def _unavailable_invocation(relative: str, purpose: str) -> dict[str, Any]:
    return {
        "source_event_identity": _canonical_hash([relative, purpose, "unsealed"]),
        "purpose": purpose,
        "adapter": None,
        "adapter_family": None,
        "provider": None,
        "model": None,
        "outcome": "interrupted",
        "attempt_count": None,
        "elapsed": {
            "kind": "invocation_adapter_elapsed_not_pure_inference",
            "seconds": None,
            "started_at": None,
            "ended_at": None,
        },
        "response_validator_seconds": None,
        "metrics": {
            "coverage": "partial",
            "reason": "unsealed_abandoned_invocation",
            "usage": {},
            "compaction": {"aggregate_count": 0, "usage": {}},
            "retry_count": 0,
            "cost": {"status": "unavailable", "kind": "unavailable", "amount": None},
        },
    }


def _validated_publication(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "status",
        "admission_outcome",
        "started_at",
        "finished_at",
        "process",
        "error_category",
    }
    process = value.get("process")
    status = value.get("status")
    admission = value.get("admission_outcome")
    error = value.get("error_category")
    exit_code = process.get("exit_code") if isinstance(process, dict) else False
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or status not in {"succeeded", "failed"}
        or admission not in {None, "accepted", "replayed", "conflict", "rejected"}
        or not isinstance(process, dict)
        or set(process) != {"exit_code"}
        or (
            exit_code is not None
            and (not isinstance(exit_code, int) or isinstance(exit_code, bool))
        )
        or error
        not in {
            None,
            "export_failed",
            "temporary_storage",
            "admission_protocol",
            "admission_rejected",
            "post_admission_failed",
        }
        or _seconds(value.get("started_at"), value.get("finished_at")) is None
    ):
        raise ValueError("invalid publication evidence")
    valid_relationship = (
        (
            status == "succeeded"
            and admission in {"accepted", "replayed"}
            and exit_code == 0
            and error is None
        )
        or (
            status == "failed"
            and admission in {"conflict", "rejected"}
            and exit_code not in {None, 0}
            and error == "admission_rejected"
        )
        or (
            status == "failed"
            and admission in {"accepted", "replayed"}
            and exit_code not in {None, 0}
            and error == "post_admission_failed"
        )
        or (
            status == "failed"
            and admission is None
            and error in {"export_failed", "temporary_storage", "admission_protocol"}
        )
    )
    if not valid_relationship:
        raise ValueError("publication protocol relationships disagree")
    return value


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
        return _invalid_source(source, error)
    identity = observed["identity"]
    assignment = observed["assignment"]
    state = observed["state"]
    root = source.resolve()
    coordinator_prefix = (
        "" if observed["coordinator"].resolve() == root else "coordinator/"
    )
    candidates: list[tuple[str, str]] = []
    abandoned_candidates: set[str] = set()
    if (root / "planner/inference").exists():
        candidates.append(("planner/inference", "acceptance_planning"))
    for entry in state["history"]:
        # Abandoned denotes coordinator progression, not absence of evidence.
        # An interrupted component may already have sealed an invocation before
        # its component output was abandoned, so discover it like any other.
        relative = f"{coordinator_prefix}{entry['directory']}/inference"
        if (root / relative).exists() or (root / relative).is_symlink():
            purpose = {
                "assessment": "finding_assessment",
                "response": "feedback_response",
            }.get(entry["component"], entry["component"])
            candidates.append((relative, purpose))
            if entry.get("outcome") == "abandoned":
                abandoned_candidates.add(relative)
    invocations = []
    seen = set()
    try:
        for relative, purpose in candidates:
            receipt_path = root / relative / "receipt.json"
            if relative in abandoned_candidates and not receipt_path.is_file():
                item = _unavailable_invocation(relative, purpose)
            else:
                item = _invocation(root, relative, purpose)
            key = item["source_event_identity"] or _canonical_hash([relative, purpose])
            if key not in seen:
                seen.add(key)
                invocations.append(item)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ExportError) as error:
        return _invalid_source(source, error, identity, assignment)
    validation_durations = []
    validation_results = []
    try:
        for entry in state["history"]:
            if (
                entry.get("component") == "validation"
                and entry.get("outcome") != "abandoned"
            ):
                output = _safe_json(
                    root / coordinator_prefix / entry["directory"] / "output.json"
                )
                if validate_component_output("validation", output) != entry.get(
                    "outcome"
                ):
                    raise ValueError("Validation output disagrees with history")
                # Reuse Export's complete component validator, including the
                # duration and process contracts, before projecting any field.
                normalize_component_output("validation", output, [])
                duration = _number(output.get("duration_seconds"))
                if "duration_seconds" in output and duration is None:
                    raise ValueError("invalid Validation duration")
                if duration is not None:
                    validation_durations.append(duration)
                validation_results.append(output["outcome"])
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        ExportError,
    ) as error:
        return _invalid_source(source, error, identity, assignment)
    receipt_metrics = [item["metrics"] for item in invocations]
    elapsed_values = [item["elapsed"]["seconds"] for item in invocations]
    invocation_seconds = (
        sum(elapsed_values)
        if elapsed_values and all(value is not None for value in elapsed_values)
        else None
    )
    known_invocation_seconds = sum(
        value for value in elapsed_values if value is not None
    )
    prep = observed.get("preparation")
    timestamps = prep.get("timestamps", {}) if isinstance(prep, dict) else {}
    run_span = _seconds(timestamps.get("started_at"), timestamps.get("finished_at"))
    prep_seconds = _seconds(timestamps.get("started_at"), timestamps.get("prepared_at"))
    publication_seconds = None
    completion_acceptance = "unavailable"
    integration_status = "unavailable"
    publication_path = observed["terminal_directory"] / "publication.json"
    if publication_path.exists() or publication_path.is_symlink():
        try:
            if publication_path.is_symlink() or not publication_path.is_file():
                raise ValueError("publication evidence is not a regular file")
            publication = _validated_publication(_safe_json(publication_path))
            publication_seconds = _seconds(
                publication["started_at"], publication["finished_at"]
            )
            if publication["admission_outcome"] is not None:
                completion_acceptance = publication["admission_outcome"]
            integration_status = publication["status"]
        except (
            OSError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as error:
            return _invalid_source(source, error, identity, assignment)
    known_nonoverlap = (
        (prep_seconds or 0) + known_invocation_seconds + sum(validation_durations)
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
    # Replay of byte-equivalent projected evidence is idempotent. Conflicting
    # evidence claiming the same stable Run identity is instead represented as
    # one fail-closed source; input ordering can never select trusted totals.
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    order: list[str] = []
    for source in sources:
        run = summarize_source(source)
        identity = run["source_identity"]
        fingerprint = _canonical_hash(run)
        if identity not in grouped:
            grouped[identity] = {}
            order.append(identity)
        grouped[identity][fingerprint] = run
    runs = []
    for identity in order:
        variants = grouped[identity]
        if len(variants) == 1:
            runs.append(next(iter(variants.values())))
            continue
        representative = next(iter(variants.values()))
        runs.append(
            {
                "source_identity": identity,
                "integrity": {
                    "status": "invalid",
                    "error": "ConflictingSourceEvidence",
                    "variant_count": len(variants),
                    "variant_sha256": sorted(variants),
                },
                "run_identity": representative.get("run_identity"),
                "work": None,
                "outcome": None,
                "inference": None,
                "timing": None,
            }
        )
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
