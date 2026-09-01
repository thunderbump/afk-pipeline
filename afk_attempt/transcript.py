"""Derive a bounded public account from private Pi Attempt events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from afk_agent import classified_agent_response_bytes

MAX_TRANSCRIPT_BYTES = 256 * 1024
_TEXT_LIMIT = 4096
_TOOLS = {"bash", "edit", "write", "read", "grep", "find", "ls"}
_STOP_REASONS = {"stop", "length", "toolUse", "error", "aborted"}
_EVENT_FIELDS = {
    "agent_start": {"type"},
    "agent_end": {"type", "willRetry"},
    "agent_settled": {"type"},
    "turn_start": {"type"},
    "turn_end": {"type"},
    "auto_retry_start": {"type", "attempt", "maxAttempts", "delayMs", "errorMessage"},
    "auto_retry_end": {"type", "attempt", "success"},
    "compaction_start": {"type", "reason"},
    "compaction_end": {
        "type",
        "reason",
        "aborted",
        "willRetry",
        "result",
        "errorMessage",
    },
    "message_end": {"type", "message"},
    "tool_execution_start": {"type", "toolCallId", "toolName", "args"},
    "tool_execution_end": {"type", "toolCallId", "toolName", "result"},
}
_KNOWN_EVENT_TYPES = set(_EVENT_FIELDS) | {
    "session",
    "message_start",
    "message_update",
    "tool_execution_update",
}


def build_attempt_transcript(
    raw: bytes,
    sanitize_text: Callable[[str], str],
    *,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
) -> dict[str, object]:
    """Validate *raw* and retain only the closed public event/tool fact catalog.

    Event bodies are private by default. Unknown metadata is represented only by
    fixed categories and counts, so attacker-controlled key and enum names never
    become public merely because the event stream is protocol-valid.
    """
    if not isinstance(raw, bytes) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("invalid Attempt transcript input")
    source = {
        "kind": "attempt_events",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not raw:
        result = _envelope(
            source, "empty", [{"sequence": 0, "event": "empty_session"}], [], 0
        )
        _require_size(result, max_bytes)
        return result

    validation = classified_agent_response_bytes(raw)
    agent = validation["agent"]
    if agent.get("status") == "error" and agent.get("error_kind") == "protocol":
        raise ValueError("validated Attempt event stream is malformed")
    try:
        text = raw.decode("utf-8")
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:  # defensive parity
        raise ValueError("validated Attempt event stream is malformed") from error

    records: list[dict[str, object]] = []
    omitted: dict[tuple[str, str], int] = {}
    omitted_sequences: set[int] = set()
    calls: dict[object, str] = {}

    def omit(reason: str, event_type: object, count: int = 1) -> None:
        if count <= 0:
            return
        name = event_type if event_type in _KNOWN_EVENT_TYPES else "unknown"
        key = reason, name
        omitted[key] = omitted.get(key, 0) + count
        omitted_sequences.add(sequence)

    for sequence, value in enumerate(events, 1):
        event_type = value.get("type")
        record: dict[str, object] | None = None
        if event_type in {
            "agent_start",
            "agent_end",
            "agent_settled",
            "turn_start",
            "turn_end",
        }:
            record = {"sequence": sequence, "event": event_type}
            if event_type == "agent_end" and isinstance(value.get("willRetry"), bool):
                record["will_retry"] = value["willRetry"]
        elif event_type == "auto_retry_start":
            record = {
                "sequence": sequence,
                "event": "provider_retry_started",
                "attempt": value["attempt"],
                "max_attempts": value["maxAttempts"],
                "delay_ms": value["delayMs"],
                "error": _safe_text(value["errorMessage"], sanitize_text),
            }
        elif event_type == "auto_retry_end":
            record = {
                "sequence": sequence,
                "event": "provider_retry_finished",
                "attempt": value["attempt"],
                "success": value["success"],
            }
        elif event_type == "compaction_start":
            record = {
                "sequence": sequence,
                "event": "provider_compaction_started",
                "reason": value["reason"],
            }
        elif event_type == "compaction_end":
            record = {
                "sequence": sequence,
                "event": "provider_compaction_finished",
                "reason": value["reason"],
                "aborted": value["aborted"],
            }
            nested = _omitted_value_count(value.get("result"))
            if "errorMessage" in value:
                nested += 1
            if nested:
                record["omitted_field_count"] = nested
                omit("event_fields_not_public", event_type, nested)
        elif event_type == "message_end":
            message = value.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                stop_reason = message.get("stopReason")
                record = {
                    "sequence": sequence,
                    "event": "assistant_message_finished",
                    "stop_reason": (
                        stop_reason if stop_reason in _STOP_REASONS else "other"
                    ),
                }
                dropped = len(set(message) - {"role", "stopReason"})
                if stop_reason not in _STOP_REASONS:
                    dropped += 1
                    omit("metadata_value_not_allowlisted", event_type)
                if dropped:
                    record["omitted_field_count"] = dropped
                    if "content" in message:
                        record["omitted_fields"] = ["content"]
                    omit("event_fields_not_public", event_type, dropped)
            else:
                omit("message_body_not_public", event_type)
        elif event_type == "tool_execution_start":
            record, dropped = _tool_start(value, sequence, sanitize_text)
            tool = value.get("toolName")
            call_id = value.get("toolCallId")
            if record is not None and isinstance(call_id, str):
                calls[call_id] = tool
            if record is None:
                omit("tool_or_arguments_not_allowlisted", event_type)
            elif dropped:
                record["omitted_field_count"] = dropped
                omit("tool_arguments_not_public", event_type, dropped)
        elif event_type == "tool_execution_end":
            call_id = value.get("toolCallId")
            tool = value.get("toolName")
            if not tool and isinstance(call_id, str):
                tool = calls.get(call_id)
            if tool in _TOOLS:
                record = {"sequence": sequence, "event": "tool_finished", "tool": tool}
                result = value.get("result")
                if isinstance(result, dict) and isinstance(result.get("isError"), bool):
                    record["is_error"] = result["isError"]
                dropped = _omitted_result_count(result)
                if dropped:
                    record["omitted_fields"] = ["result"]
                    record["omitted_field_count"] = dropped
                    omit("tool_result_not_public", event_type, dropped)
            else:
                omit("tool_or_arguments_not_allowlisted", event_type)
        elif event_type in {
            "session",
            "message_start",
            "message_update",
            "tool_execution_update",
        }:
            omit("event_payload_not_public", event_type)
        else:
            omit("event_not_allowlisted", event_type)

        if record is not None:
            extra = len(set(value) - _EVENT_FIELDS[event_type])
            if extra:
                record["omitted_field_count"] = (
                    record.get("omitted_field_count", 0) + extra
                )
                omit("event_fields_not_public", event_type, extra)
            records.append(record)

    omissions = [
        {"reason": reason, "event_type": event_type, "count": count}
        for (reason, event_type), count in omitted.items()
    ]
    result = _envelope(
        source, "complete", records, omissions, len(events), len(omitted_sequences)
    )
    return _truncate(result, max_bytes)


def _tool_start(value, sequence, sanitize_text):
    tool = value.get("toolName")
    args = value.get("args")
    if tool not in _TOOLS or not isinstance(args, dict):
        return None, 0
    record = {"sequence": sequence, "event": "tool_started", "tool": tool}
    omitted_names = []
    if tool == "bash":
        if not isinstance(args.get("command"), str):
            return None, 0
        record["command"] = _safe_text(args["command"], sanitize_text)
        allowed = {"command"}
    elif tool in {"edit", "write", "read", "grep", "find", "ls"}:
        path = args.get("path")
        if path is not None:
            if not isinstance(path, str):
                return None, 0
            record["path"] = _safe_text(path, sanitize_text)
        if tool == "edit":
            record["operation"] = "replace"
            allowed = {"path", "oldText", "newText"}
            omitted_names = [key for key in ("oldText", "newText") if key in args]
        elif tool == "write":
            content = args.get("content")
            if isinstance(content, str):
                record["content_bytes"] = len(content.encode())
                record["content_lines"] = len(content.splitlines())
            allowed = {"path", "content"}
            omitted_names = ["content"] if "content" in args else []
        else:
            for key in ("offset", "limit"):
                if isinstance(args.get(key), int) and not isinstance(args[key], bool):
                    record[key] = args[key]
            allowed = {"path", "offset", "limit"}
    if omitted_names:
        record["omitted_fields"] = omitted_names
    return record, len(omitted_names) + len(set(args) - allowed)


def _omitted_result_count(result):
    if isinstance(result, dict):
        return len(set(result) - {"isError"})
    return 1 if result is not None else 0


def _omitted_value_count(value):
    if isinstance(value, dict):
        return len(value)
    return 1 if value is not None else 0


def _safe_text(value, sanitizer):
    try:
        public = sanitizer(value)
    except Exception as error:
        raise ValueError("unsafe transcript content was refused") from error
    if not isinstance(public, str):
        raise TypeError("unsafe transcript content was refused")
    if len(public) > _TEXT_LIMIT:
        public = public[:_TEXT_LIMIT] + "[compacted]"
    return public


def _envelope(source, status, records, omissions, event_count, omitted_event_count=0):
    omitted_item_count = sum(item["count"] for item in omissions)
    return {
        "schema_version": 1,
        "kind": "attempt_session_transcript",
        "ownership": "attempt",
        "source": source,
        "policy": {
            "retained": [
                "ordered lifecycle and provider retry facts",
                "allowlisted tool names, commands, paths, and operation counts",
            ],
            "omitted": [
                "prompts and message bodies",
                "tool results and file content",
                "unknown events, tools, arguments, fields, and metadata values",
            ],
        },
        "status": status,
        "records": records,
        "omissions": omissions,
        "summary": {
            "source_event_count": event_count,
            "published_record_count": len(records),
            "omitted_event_count": omitted_event_count,
            "omitted_item_count": omitted_item_count,
        },
    }


def encode_attempt_transcript(value):
    """Serialize exactly as the downloadable public artifact is written."""
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _require_size(value, max_bytes):
    if len(encode_attempt_transcript(value)) > max_bytes:
        raise ValueError("Attempt transcript size limit is too small")


def _truncate(result, max_bytes):
    if len(encode_attempt_transcript(result)) <= max_bytes:
        return result
    result["status"] = "truncated"
    removed = 0
    marker = {
        "sequence": 0,
        "event": "transcript_truncated",
        "omitted_records": 0,
        "limit_bytes": max_bytes,
    }
    result["records"].append(marker)
    while (
        len(encode_attempt_transcript(result)) > max_bytes
        and len(result["records"]) > 1
    ):
        result["records"].pop(-2)
        removed += 1
        marker["omitted_records"] = removed
        result["summary"]["published_record_count"] = len(result["records"])
    marker["sequence"] = (
        result["records"][-2]["sequence"] + 1 if len(result["records"]) > 1 else 0
    )
    _require_size(result, max_bytes)
    return result
