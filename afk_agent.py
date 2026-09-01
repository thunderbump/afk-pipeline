"""Interpret the durable JSON event protocol emitted by AFK agent adapters."""

import json
import math
from pathlib import Path

# Pi defaults to three model retries. Keep interpretation bounded even when an
# untrusted event stream advertises a larger maximum.
MAX_AUTO_RETRIES = 3


def agent_response(events_path: Path) -> dict[str, object]:
    return agent_response_bytes(events_path.read_bytes())


def agent_response_bytes(data: bytes) -> dict[str, object]:
    """Interpret Pi JSONL bytes using the stable public result shape."""
    result = classified_agent_response_bytes(data)
    agent = result["agent"]
    if agent.get("status") == "error":
        agent.pop("error_kind", None)
    return result


def classified_agent_response_bytes(data: bytes) -> dict[str, object]:
    """Interpret Pi JSONL bytes and distinguish protocol and provider errors."""
    state = "segment"
    saw_final_end = False
    saw_settled = False
    terminal_compaction = None
    compaction_reason = None
    retry_count = 0
    retry_completed = False
    retry_response_seen = False
    terminal_message = None
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return protocol_error("invalid agent event encoding")
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return protocol_error("invalid agent event JSON")
        if not isinstance(event, dict):
            return protocol_error("invalid agent event JSON")
        event_type = event.get("type")

        if saw_final_end:
            if saw_settled:
                return protocol_error("events follow agent_end")
            if terminal_compaction is None:
                if event_type == "agent_settled":
                    saw_settled = True
                    continue
                if event_type == "compaction_start" and valid_compaction_start(event):
                    terminal_compaction = "started"
                    compaction_reason = event["reason"]
                    continue
                if event_type in {"compaction_start", "compaction_end"}:
                    return protocol_error("invalid terminal compaction event sequence")
                return protocol_error("events follow agent_end")
            if terminal_compaction == "started":
                if event_type == "compaction_end" and valid_compaction_end(
                    event, compaction_reason
                ):
                    terminal_compaction = "ended"
                    continue
                return protocol_error("invalid terminal compaction event sequence")
            if event_type == "agent_settled":
                saw_settled = True
                continue
            return protocol_error("invalid terminal compaction event sequence")
        if event_type in {"compaction_start", "compaction_end"}:
            return protocol_error("invalid terminal compaction event sequence")
        if event_type == "agent_settled":
            return protocol_error("agent_settled precedes agent_end")

        if state == "retry_start":
            if event_type != "auto_retry_start" or not valid_retry_start(
                event, retry_count
            ):
                return protocol_error("invalid auto-retry event sequence")
            state = "agent_start"
            continue
        if state == "agent_start":
            if event_type != "agent_start":
                return protocol_error("invalid auto-retry event sequence")
            state = "segment"
            continue
        if event_type == "auto_retry_start":
            return protocol_error("invalid auto-retry event sequence")

        if event_type == "message_end":
            message = event.get("message")
            if not valid_message(message):
                return protocol_error("invalid agent event JSON")
            if message["role"] == "assistant":
                stop_reason = message["stopReason"]
                if (
                    retry_count
                    and not retry_completed
                    and stop_reason not in {"error", "aborted"}
                ):
                    retry_response_seen = True
                if stop_reason != "toolUse":
                    if terminal_message is not None:
                        return protocol_error("conflicting terminal assistant messages")
                    terminal_message = message
            continue

        if event_type == "auto_retry_end":
            if (
                retry_count == 0
                or retry_completed
                or event.get("success") is not True
                or not valid_retry_attempt(event.get("attempt"), retry_count)
                or not retry_response_seen
                or (
                    terminal_message is not None
                    and terminal_message.get("stopReason") in {"error", "aborted"}
                )
            ):
                return protocol_error("invalid auto-retry event sequence")
            retry_completed = True
            continue

        if event_type == "agent_end":
            if "willRetry" in event and not isinstance(event["willRetry"], bool):
                return protocol_error("invalid agent event JSON")
            if event.get("willRetry") is True:
                if (
                    retry_completed
                    or terminal_message is None
                    or terminal_message.get("stopReason") != "error"
                    or retry_count >= MAX_AUTO_RETRIES
                ):
                    return protocol_error("invalid auto-retry event sequence")
                retry_count += 1
                retry_response_seen = False
                terminal_message = None
                state = "retry_start"
                continue
            if retry_count and not retry_completed:
                return protocol_error("invalid auto-retry event sequence")
            saw_final_end = True

    if terminal_compaction is not None and not saw_settled:
        return protocol_error("invalid terminal compaction event sequence")
    if (
        not saw_final_end
        or terminal_message is None
        or state != "segment"
        or (retry_count > 0 and not saw_settled)
    ):
        return protocol_error("agent event stream did not complete")
    if terminal_message.get("stopReason") == "error":
        return provider_error(terminal_message.get("errorMessage", "agent error"))
    if terminal_message.get("stopReason") == "aborted":
        return {"agent": {"status": "aborted"}, "text": None}
    return {
        "agent": {"status": "completed"},
        "text": "".join(
            part["text"]
            for part in terminal_message.get("content", [])
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ),
    }


def valid_message(message: object) -> bool:
    if not isinstance(message, dict) or not isinstance(message.get("role"), str):
        return False
    if message["role"] != "assistant":
        return True
    stop_reason = message.get("stopReason")
    content = message.get("content", [])
    if not isinstance(stop_reason, str) or not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            return False
        if part["type"] == "text" and not isinstance(part.get("text"), str):
            return False
    return not (
        stop_reason == "error"
        and "errorMessage" in message
        and not isinstance(message["errorMessage"], str)
    )


def valid_compaction_start(event: dict[str, object]) -> bool:
    return event.get("reason") in {"manual", "threshold", "overflow"}


def valid_compaction_end(event: dict[str, object], reason: object) -> bool:
    return (
        event.get("reason") == reason
        and isinstance(event.get("aborted"), bool)
        and event.get("willRetry") is False
        and ("result" not in event or isinstance(event["result"], dict))
        and ("errorMessage" not in event or isinstance(event["errorMessage"], str))
    )


def valid_retry_start(event: dict[str, object], attempt: int) -> bool:
    max_attempts = event.get("maxAttempts")
    delay_ms = event.get("delayMs")
    return (
        valid_retry_attempt(event.get("attempt"), attempt)
        and isinstance(max_attempts, int)
        and not isinstance(max_attempts, bool)
        and attempt <= max_attempts
        and isinstance(delay_ms, (int, float))
        and not isinstance(delay_ms, bool)
        and math.isfinite(delay_ms)
        and delay_ms >= 0
        and isinstance(event.get("errorMessage"), str)
    )


def valid_retry_attempt(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def protocol_error(message: str) -> dict[str, object]:
    return _classified_error(message, "protocol")


def provider_error(message: str) -> dict[str, object]:
    return _classified_error(message, "provider")


def _classified_error(message: str, error_kind: str) -> dict[str, object]:
    return {
        "agent": {
            "status": "error",
            "error": message,
            "error_kind": error_kind,
        },
        "text": None,
    }
