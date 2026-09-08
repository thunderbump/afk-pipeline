"""Read-only metrics projection over authenticated AFK evidence.

Only metadata, timings, counts, and usage fields are retained.  Event streams are
processed one line at a time and event content is never copied into the report.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from afk_coordinate.contract import validate_component_output
from afk_export import (
    ExportError,
    ExportUsageError,
    hash_file_beneath,
    load_source,
    normalize_component_output,
    open_directory_beneath,
    open_file_beneath,
    read_json_at,
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
MAX_REPORTED_IDENTITIES = 128

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


def parse_pi_events(
    path: Path | int, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Stream Pi JSONL and optionally bind parsing to an already-open artifact.

    Descriptor input is used by report generation so hashing and parsing observe
    the same inode. Path input remains available for standalone fixture tests.
    """
    usage: dict[str, float | int] = {}
    compact_usage: dict[str, float | int] = {}
    compact_cost = 0.0
    compaction_has_cost = False
    cost = 0.0
    has_cost = False
    cost_measurements = 0
    finalized = 0
    missing = 0
    categories_partial = False
    retries = 0
    compactions = 0
    digest = hashlib.sha256()
    if isinstance(path, int):
        descriptor = os.dup(path)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            os.close(descriptor)
            raise ValueError("Pi event evidence is not a regular file")
    else:
        descriptor = None
        before = None
    # Exact event de-duplication can itself be attacker-controlled input. Keep
    # its growing index in a temporary SQLite database rather than Python sets,
    # so resident memory does not grow with a large retained stream.
    with (
        closing(sqlite3.connect("")) as dedupe,
        (
            os.fdopen(descriptor, "rb")
            if descriptor is not None
            else Path(path).open("rb")
        ) as stream,
    ):
        dedupe.executescript(
            """
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-1024;
            CREATE TABLE seen (kind TEXT NOT NULL, identity BLOB NOT NULL,
                               PRIMARY KEY (kind, identity)) WITHOUT ROWID;
            CREATE TABLE retry_pending (attempt BLOB PRIMARY KEY, copies INTEGER NOT NULL)
                WITHOUT ROWID;
            CREATE TABLE identities (identity BLOB PRIMARY KEY, provider TEXT, model TEXT)
                WITHOUT ROWID;
            """
        )

        def stable_key(value: Any) -> bytes:
            return hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).digest()

        def first_seen(kind: str, value: Any) -> bool:
            cursor = dedupe.execute(
                "INSERT OR IGNORE INTO seen VALUES (?, ?)", (kind, stable_key(value))
            )
            return cursor.rowcount == 1

        line_number = 0
        while line := stream.readline(MAX_JSONL_RECORD_BYTES + 1):
            digest.update(line)
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
                # Attempt numbers restart for each provider request. Pair each
                # end with an open start, rather than globally de-duplicating on
                # that number; an end without a start is old but valid evidence.
                attempt_key = stable_key(event.get("attempt"))
                pending = dedupe.execute(
                    "SELECT copies FROM retry_pending WHERE attempt = ?",
                    (attempt_key,),
                ).fetchone()
                if kind == "auto_retry_start":
                    retries += 1
                    if pending is None:
                        dedupe.execute(
                            "INSERT INTO retry_pending VALUES (?, 1)", (attempt_key,)
                        )
                    else:
                        dedupe.execute(
                            "UPDATE retry_pending SET copies = copies + 1 WHERE attempt = ?",
                            (attempt_key,),
                        )
                elif pending is None:
                    retries += 1
                elif pending[0] == 1:
                    dedupe.execute(
                        "DELETE FROM retry_pending WHERE attempt = ?", (attempt_key,)
                    )
                else:
                    dedupe.execute(
                        "UPDATE retry_pending SET copies = copies - 1 WHERE attempt = ?",
                        (attempt_key,),
                    )
            if kind == "message_end":
                message, raw_usage = _message_usage(event)
                if message is None:
                    continue
                identity = message.get("id")
                if (
                    isinstance(identity, str)
                    and identity
                    and not first_seen("message", identity)
                ):
                    continue
                finalized += 1
                provider = _identity_label(message.get("provider"))
                model = _identity_label(message.get("model"))
                if provider is not None or model is not None:
                    identity_key = stable_key([provider, model])
                    dedupe.execute(
                        "INSERT OR IGNORE INTO identities VALUES (?, ?, ?)",
                        (identity_key, provider, model),
                    )
                measured = _usage(raw_usage)
                if not set(TOKEN_FIELDS[:-1]).issubset(measured):
                    categories_partial = True
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
                identity = event.get("id")
                # No upstream id is guaranteed. Distinct un-identified events
                # may be equal aggregates, so only an upstream id deduplicates.
                if identity is not None and not first_seen("compaction", identity):
                    continue
                # A compaction is represented work even when Pi omitted all of
                # its measurements. Retain the event in the coverage denominator
                # so a measured final message cannot make that omission vanish.
                compactions += 1
                if not set(TOKEN_FIELDS[:-1]).issubset(measured):
                    categories_partial = True
                if measured:
                    _add(compact_usage, measured)
                else:
                    missing += 1
                if amount is not None:
                    compact_cost += float(amount)
                    compaction_has_cost = True
                    cost_measurements += 1
        if before is not None:
            after = os.fstat(stream.fileno())
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise ValueError("Pi event evidence changed while being parsed")
        identity_rows = dedupe.execute(
            "SELECT provider, model FROM identities ORDER BY provider, model LIMIT ?",
            (MAX_REPORTED_IDENTITIES + 1,),
        ).fetchall()
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise ValueError("Pi event evidence hash disagrees with receipt")
    identities_complete = len(identity_rows) <= MAX_REPORTED_IDENTITIES
    identity_rows = identity_rows[:MAX_REPORTED_IDENTITIES]
    partial = missing > 0 or retries > 0
    coverage = (
        "partial"
        if partial or categories_partial
        else ("complete" if finalized or compactions else "unavailable")
    )
    cost_status = (
        "unavailable"
        if not has_cost and not compaction_has_cost
        else "partial"
        if cost_measurements < finalized + compactions or partial
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
        "identity_coverage": (
            "unavailable"
            if not identity_rows
            else ("complete" if identities_complete else "partial")
        ),
        "identities": [
            {"provider": provider, "model": model} for provider, model in identity_rows
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
        seconds = (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()
        return seconds if seconds >= 0 else None
    except (TypeError, ValueError):
        return None


def _union_seconds(intervals: list[tuple[Any, Any]]) -> float:
    """Return the duration of the union of valid timestamp intervals."""
    parsed = []
    for start, end in intervals:
        if _seconds(start, end) is None:
            continue
        parsed.append(
            (
                datetime.fromisoformat(start.replace("Z", "+00:00")),
                datetime.fromisoformat(end.replace("Z", "+00:00")),
            )
        )
    parsed.sort()
    total = 0.0
    left = right = None
    for begin, finish in parsed:
        if left is None:
            left, right = begin, finish
        elif begin > right:
            total += (right - left).total_seconds()
            left, right = begin, finish
        elif finish > right:
            right = finish
    if left is not None:
        total += (right - left).total_seconds()
    return total


def _nonoverlapping_seconds(
    start: Any, end: Any, occupied: list[tuple[Any, Any]]
) -> float | None:
    """Return an interval's duration excluding the union of known overlaps."""
    total = _seconds(start, end)
    if total is None:
        return None
    begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
    finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
    clips = []
    for other_start, other_end in occupied:
        if _seconds(other_start, other_end) is None:
            continue
        left = max(begin, datetime.fromisoformat(other_start.replace("Z", "+00:00")))
        right = min(finish, datetime.fromisoformat(other_end.replace("Z", "+00:00")))
        if right > left:
            clips.append((left, right))
    clips.sort()
    overlap = 0.0
    cursor = None
    for left, right in clips:
        if cursor is None or left > cursor:
            overlap += (right - left).total_seconds()
            cursor = right
        elif right > cursor:
            overlap += (right - cursor).total_seconds()
            cursor = right
    return total - overlap


def _safe_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def _safe_evidence_json(root: Path, relative: str, name: str) -> dict[str, Any]:
    """Read JSON through Export's no-follow, real-directory evidence boundary."""
    descriptor = open_directory_beneath(root, relative)
    try:
        value = read_json_at(descriptor, name)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def _safe_optional_evidence_json(
    root: Path, relative: str, name: str
) -> dict[str, Any] | None:
    """Atomically open optional JSON without following mutable path entries."""
    descriptor = (
        open_directory_beneath(root, relative)
        if relative
        else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    )
    try:
        try:
            value = read_json_at(descriptor, name)
        except FileNotFoundError:
            return None
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise TypeError("expected JSON object")
    return value


def _safe_evidence_hash(root: Path, relative: str, name: str) -> str:
    descriptor = open_directory_beneath(root, relative)
    try:
        digest, _size = hash_file_beneath(descriptor, name)
        return digest
    finally:
        os.close(descriptor)


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


def _aggregate_measurement_coverage(
    coverages: list[str], has_measurements: bool
) -> str:
    """Combine component coverage without presenting known subsets as totals."""
    if not has_measurements:
        return "unavailable"
    if coverages and all(value == "complete" for value in coverages):
        return "complete"
    return "partial"


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


def _verify_unsupported_receipt(
    root: Path,
    relative: str,
    receipt: dict[str, Any],
    invocation: dict[str, Any],
    purpose: str,
) -> None:
    """Verify runtime-common identity while declining adapter-specific metrics."""
    identity = receipt.get("identity")
    adapter = invocation.get("adapter")
    hashes = receipt.get("hashes")
    timing = receipt.get("timing")
    adapter_identity = adapter.get("identity") if isinstance(adapter, dict) else None
    attempts = receipt.get("attempts")
    attempt_count = receipt.get("attempt_count")
    outcome = receipt.get("outcome")
    if (
        receipt.get("schema_version") != 1
        or invocation.get("schema_version") != 1
        or invocation.get("purpose") != purpose
        or not isinstance(identity, dict)
        or identity.get("runtime") != "afk-inference-v1"
        or not isinstance(adapter, dict)
        or not isinstance(adapter.get("kind"), str)
        or not isinstance(adapter_identity, str)
        or identity.get("adapter") != adapter_identity
        or not isinstance(hashes, dict)
        or not isinstance(hashes.get("invocation_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", hashes["invocation_sha256"])
        or _safe_evidence_hash(root, relative, "invocation.json")
        != hashes["invocation_sha256"]
        or not isinstance(timing, dict)
        or _number(timing.get("duration_seconds")) is None
        or _seconds(timing.get("started_at"), timing.get("ended_at")) is None
        or outcome
        not in {
            "succeeded",
            "response_rejected",
            "adapter_failed",
            "validator_failed",
            "timed_out",
            "interrupted",
        }
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
        or not isinstance(attempts, list)
        or attempt_count != len(attempts)
    ):
        raise ValueError("unsupported adapter receipt identity disagrees")
    for index, attempt in enumerate(attempts, 1):
        if (
            not isinstance(attempt, dict)
            or attempt.get("attempt_number") != index
            or _number(attempt.get("duration_seconds")) is None
        ):
            raise ValueError("unsupported adapter attempt contract disagrees")


def _validator_timing(attempts: list[Any]) -> tuple[float | None, str]:
    """Project measured validator time and explicitly qualify its coverage."""
    values = []
    relevant = 0
    missing = 0
    for attempt in attempts:
        validation = attempt.get("validation") if isinstance(attempt, dict) else None
        if not isinstance(validation, dict):
            continue
        relevant += 1
        if "validator_duration_seconds" not in validation:
            missing += 1
            continue
        value = _number(validation["validator_duration_seconds"])
        if value is None:
            raise ValueError("receipt validator timing is invalid")
        values.append(float(value))
    if not values:
        return None, "unavailable"
    return sum(values), "partial" if missing or len(values) < relevant else "complete"


def _validator_seconds(attempts: list[Any]) -> float | None:
    """Sum available response-validator measurements, preserving measured zero."""
    return _validator_timing(attempts)[0]


def _validate_pi_metric_receipt(
    receipt: dict[str, Any], invocation: dict[str, Any]
) -> None:
    """Validate runtime fields newly treated as trusted report evidence."""
    timing = receipt.get("timing")
    attempts = receipt.get("attempts")
    outcome = receipt.get("outcome")
    count = receipt.get("attempt_count")
    validation = receipt.get("validation")
    if (
        not isinstance(timing, dict)
        or not isinstance(invocation, dict)
        or (duration := _number(timing.get("duration_seconds"))) is None
        or (timeout := _number(timing.get("timeout_seconds"))) is None
        or timeout <= 0
        or timeout != _number(invocation.get("timeout_seconds"))
        or (wall := _seconds(timing.get("started_at"), timing.get("ended_at"))) is None
        or not math.isclose(float(duration), wall, rel_tol=0.01, abs_tol=0.1)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(attempts, list)
        or count != len(attempts)
        or outcome
        not in (
            "succeeded",
            "response_rejected",
            "adapter_failed",
            "validator_failed",
            "timed_out",
            "interrupted",
        )
        or not isinstance(validation, dict)
        or validation.get("status")
        not in (
            "not_run",
            "accepted",
            "response_rejected",
            "validator_failed",
            "timed_out",
            "interrupted",
        )
    ):
        raise ValueError("Pi receipt outcome or timing contract is invalid")
    validation_statuses = (
        "accepted",
        "response_rejected",
        "validator_failed",
        "timed_out",
        "interrupted",
    )
    for index, attempt in enumerate(attempts, 1):
        attempt_validation = (
            attempt.get("validation") if isinstance(attempt, dict) else None
        )
        if (
            not isinstance(attempt, dict)
            or attempt.get("attempt_number") != index
            or _number(attempt.get("duration_seconds")) is None
            or not isinstance(attempt.get("protocol"), dict)
            or not isinstance(attempt["protocol"].get("status"), str)
            or (
                attempt_validation is not None
                and (
                    not isinstance(attempt_validation, dict)
                    or attempt_validation.get("status") not in validation_statuses
                    or attempt_validation.get("attempt_number") != index
                )
            )
        ):
            raise ValueError("Pi receipt attempt contract is invalid")
    validator_seconds, _coverage = _validator_timing(attempts)
    if validator_seconds is not None and validator_seconds > float(duration):
        raise ValueError("Pi receipt validator timing exceeds invocation elapsed time")
    expected_protocol = (
        attempts[-1]["protocol"] if attempts else {"status": "not_started"}
    )
    expected_validation = {
        "succeeded": "accepted",
        "response_rejected": "response_rejected",
        "validator_failed": "validator_failed",
    }.get(outcome)
    if receipt.get("protocol") != expected_protocol or (
        expected_validation is not None
        and validation.get("status") != expected_validation
    ):
        raise ValueError("Pi receipt terminal contract is invalid")


def _invocation(root: Path, relative: str, purpose: str) -> dict[str, Any]:
    receipt = _safe_evidence_json(root, relative, "receipt.json")
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
        "response_validator_coverage": "unavailable",
    }
    attempts = (
        receipt.get("attempts") if isinstance(receipt.get("attempts"), list) else []
    )
    if family != "pi":
        # Fixture receipts have a repository-known contract and can be fully
        # authenticated. Other retained adapter families are deliberately an
        # observational reporting boundary: preserve the invocation, but do not
        # interpret adapter-specific event streams as usage or cost evidence.
        invocation = _safe_evidence_json(root, relative, "invocation.json")
        adapter = invocation.get("adapter")
        if isinstance(adapter, dict) and adapter.get("kind") == "fixture":
            _verify_generic_receipt(root / relative, receipt)
            (
                base["response_validator_seconds"],
                base["response_validator_coverage"],
            ) = _validator_timing(attempts)
        else:
            _verify_unsupported_receipt(root, relative, receipt, invocation, purpose)
            # Adapter-specific validator payloads are not authenticated by the
            # common unsupported-adapter boundary, so do not even parse them.
        return {
            **base,
            "metrics": {
                "coverage": "unavailable",
                "reason": "unsupported_adapter",
                "usage": {},
                "compaction": {"aggregate_count": 0, "usage": {}},
                "retry_count": max(receipt["attempt_count"] - 1, 0),
                "cost": {
                    "status": "unavailable",
                    "kind": "unavailable",
                    "amount": None,
                },
            },
        }

    # Keep Export's authenticated directory open through parsing. Each stream is
    # opened without following links and parsed from the same descriptor whose
    # bytes are checked against the exact validated receipt.
    def consume_events(
        directory_descriptor: int,
        authenticated: dict[str, Any],
        authenticated_invocation: dict[str, Any],
    ):
        _validate_pi_metric_receipt(authenticated, authenticated_invocation)
        values = []
        authenticated_attempts = authenticated.get("attempts", [])
        for attempt in authenticated_attempts:
            if not isinstance(attempt, dict):
                continue
            artifacts = attempt.get("artifacts")
            event_name = (
                artifacts.get("events") if isinstance(artifacts, dict) else None
            )
            expected = (
                artifacts.get("events_sha256") if isinstance(artifacts, dict) else None
            )
            if event_name is None and expected is None:
                continue
            if (
                not isinstance(event_name, str)
                or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
            ):
                raise ValueError("Pi event receipt contract is invalid")
            descriptor = open_file_beneath(directory_descriptor, event_name)
            try:
                values.append(parse_pi_events(descriptor, expected))
            finally:
                os.close(descriptor)
        return authenticated, values

    _catalog, consumed = receipt_bound_inference_artifacts(
        root,
        relative,
        purpose,
        authenticated_context_consumer=consume_events,
    )
    receipt, parsed = consumed
    attempts = receipt.get("attempts", [])
    identity = receipt["identity"]
    timing = receipt["timing"]
    base.update(
        {
            "source_event_identity": receipt["hashes"]["invocation_sha256"],
            "adapter": _identity_label(identity.get("adapter")),
            "adapter_family": _identity_label(identity.get("adapter_family")),
            "provider": _identity_label(identity.get("provider")),
            "model": _identity_label(identity.get("model")),
            "outcome": receipt.get("outcome"),
            "attempt_count": receipt.get("attempt_count"),
            "elapsed": {
                "kind": "invocation_adapter_elapsed_not_pure_inference",
                "seconds": _number(timing.get("duration_seconds")),
                "started_at": timing.get("started_at"),
                "ended_at": timing.get("ended_at"),
            },
        }
    )
    (
        base["response_validator_seconds"],
        base["response_validator_coverage"],
    ) = _validator_timing(attempts)
    if (
        any(item["coverage"] == "partial" for item in parsed)
        or len(parsed) < len(attempts)
        or receipt.get("outcome") not in {"succeeded", None}
    ):
        # A failed/retried/aborted request can legitimately omit usage even when
        # a later finalized message has measurements.
        merged_coverage = "partial"
    elif parsed and all(item["coverage"] == "complete" for item in parsed):
        merged_coverage = "complete"
    else:
        merged_coverage = "unavailable"
    merged = {
        "finalized_requests": sum(item["finalized_requests"] for item in parsed),
        "request_count_exact": merged_coverage == "complete"
        and all(item["request_count_exact"] for item in parsed),
        # Runtime attempts represent distinct adapter invocations. Pi's
        # auto_retry events represent retries *within* those attempts, so both
        # sources are additive rather than alternatives.
        "retry_count": max(receipt["attempt_count"] - 1, 0)
        + sum(item["retry_count"] for item in parsed),
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
    uncertain_cost_coverage = (
        merged_coverage != "complete"
        or merged["retry_count"] > 0
        or receipt.get("outcome") not in {"succeeded", None}
    )
    cost_status = (
        "unavailable"
        if not costs
        else "partial"
        if cost_groups < measured_groups or uncertain_cost_coverage
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
    identity_coverages = [
        item.get("identity_coverage", "unavailable") for item in parsed
    ]
    identities_complete = bool(identity_coverages) and all(
        value == "complete" for value in identity_coverages
    )
    merged["identity_coverage"] = (
        "partial"
        if any(value == "partial" for value in identity_coverages)
        else ("complete" if identities_complete else "unavailable")
    )
    observed_identities = {
        (identity["provider"], identity["model"])
        for item in parsed
        for identity in item["identities"]
    }
    if identities_complete and len(observed_identities) == 1:
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
        "response_validator_coverage": "unavailable",
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
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        json.JSONDecodeError,
        ExportError,
    ) as error:
        return _invalid_source(source, error, identity, assignment)
    validation_durations = []
    validation_intervals: list[tuple[str, str]] = []
    validation_duration_missing = 0
    validation_results = []
    try:
        for entry in state["history"]:
            if (
                entry.get("component") == "validation"
                and entry.get("outcome") != "abandoned"
            ):
                component_relative = (
                    f"{coordinator_prefix}{entry['directory']}"
                ).rstrip("/")
                output = _safe_evidence_json(root, component_relative, "output.json")
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
                else:
                    validation_duration_missing += 1
                validation_start = output.get("started_at")
                validation_end = output.get("finished_at")
                if validation_start is not None or validation_end is not None:
                    if _seconds(validation_start, validation_end) is None:
                        raise ValueError("invalid Validation timestamp interval")
                    validation_intervals.append((validation_start, validation_end))
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
    prep = observed.get("preparation")
    timestamps = prep.get("timestamps", {}) if isinstance(prep, dict) else {}
    run_started_at = timestamps.get("started_at")
    run_end_candidates = [timestamps.get("finished_at")]
    prep_seconds = _seconds(run_started_at, timestamps.get("prepared_at"))
    publication_seconds = None
    publication_nonoverlap_seconds = None
    completion_acceptance = "unavailable"
    integration_status = "unavailable"
    try:
        terminal_directory = Path(observed["terminal_directory"]).absolute()
        terminal_relative_path = terminal_directory.relative_to(root)
        terminal_relative = (
            terminal_relative_path.as_posix() if terminal_relative_path.parts else ""
        )
        publication_value = _safe_optional_evidence_json(
            root, terminal_relative, "publication.json"
        )
        if publication_value is not None:
            publication = _validated_publication(publication_value)
            publication_seconds = _seconds(
                publication["started_at"], publication["finished_at"]
            )
            occupied = [(run_started_at, timestamps.get("prepared_at"))]
            occupied.extend(
                (item["elapsed"].get("started_at"), item["elapsed"].get("ended_at"))
                for item in invocations
                if isinstance(item.get("elapsed"), dict)
            )
            occupied.extend(validation_intervals)
            publication_nonoverlap_seconds = _nonoverlapping_seconds(
                publication["started_at"], publication["finished_at"], occupied
            )
            run_end_candidates.append(publication["finished_at"])
            # Publication does not prove completion acceptance or Git integration.
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        ExportError,
    ) as error:
        return _invalid_source(source, error, identity, assignment)
    # A preparation's finished_at seals the original coordinator only. A
    # selected continuation can contain later invocations (and publication), so
    # extend the wall span to the latest authenticated end timestamp.
    run_end_candidates.extend(
        item["elapsed"].get("ended_at")
        for item in invocations
        if isinstance(item.get("elapsed"), dict)
    )
    run_end_candidates.extend(end for _start, end in validation_intervals)
    candidate_spans = [
        value
        for end in run_end_candidates
        if (value := _seconds(run_started_at, end)) is not None
    ]
    run_span = max(candidate_spans) if candidate_spans else None
    # Attribute wall time from the union of authenticated intervals. Summing
    # component durations would double-count concurrent retries, continuations,
    # Validation, preparation, or publication.
    measured_intervals: list[tuple[Any, Any]] = [
        (run_started_at, timestamps.get("prepared_at"))
    ]
    measured_intervals.extend(
        (item["elapsed"].get("started_at"), item["elapsed"].get("ended_at"))
        for item in invocations
        if isinstance(item.get("elapsed"), dict)
    )
    measured_intervals.extend(validation_intervals)
    if publication_value is not None:
        measured_intervals.append(
            (publication_value.get("started_at"), publication_value.get("finished_at"))
        )
    known_active_wall_seconds = _union_seconds(measured_intervals)
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
    if publication_seconds is not None:
        active_intervals.append(
            {
                "kind": "publication",
                "seconds": publication_seconds,
                "nonoverlapping_seconds": publication_nonoverlap_seconds,
            }
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
    validator_durations = [
        duration
        for item in invocations
        if (duration := item["response_validator_seconds"]) is not None
    ]
    validator_coverages = [
        item.get(
            "response_validator_coverage",
            "complete"
            if item.get("response_validator_seconds") is not None
            else "unavailable",
        )
        for item in invocations
    ]
    response_validator_coverage = _aggregate_measurement_coverage(
        validator_coverages, bool(validator_durations)
    )
    repository_validation_coverage = (
        "partial"
        if validation_durations and validation_duration_missing
        else "complete"
        if validation_durations
        else "unavailable"
    )
    total_usage = _sum_usage(receipt_metrics)
    total_compaction_usage = _sum_usage(receipt_metrics, "compaction")
    usage_coverages = [
        metrics.get("coverage", "unavailable") for metrics in receipt_metrics
    ]
    if any(value == "partial" for value in usage_coverages):
        usage_coverage = "partial"
    elif not total_usage and not total_compaction_usage:
        usage_coverage = "unavailable"
    elif usage_coverages and all(value == "complete" for value in usage_coverages):
        usage_coverage = "complete"
    else:
        usage_coverage = "partial"
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
            "coordinator_decision": observed["output"].get("decision"),
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
                "usage": total_usage,
                "compaction_usage": total_compaction_usage,
                "usage_coverage": usage_coverage,
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
            "repository_validation_coverage": repository_validation_coverage,
            "response_validator_seconds": sum(validator_durations)
            if validator_durations
            else None,
            "response_validator_coverage": response_validator_coverage,
            "deterministic_steps": {
                "Validation": sum(validation_durations)
                if validation_durations
                else "unavailable",
                "Change": "unavailable",
                "Iteration": "unavailable",
            },
            "continuation_wait_gaps": "unavailable",
            "unattributed_seconds": (
                max(0, run_span - known_active_wall_seconds)
                if run_span is not None and known_active_wall_seconds <= run_span
                else None
            ),
            "overlap_note": "unattributed excludes the union of authenticated preparation, invocation, Validation, and publication intervals; nested response validation is not subtracted again",
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
                    f"unavailable {label}"
                    if left["work"].get(field) is None
                    or right["work"].get(field) is None
                    else f"different {label}"
                    for field, label in names.items()
                    if left["work"].get(field) is None
                    or right["work"].get(field) is None
                    or left["work"].get(field) != right["work"].get(field)
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
