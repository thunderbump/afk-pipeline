"""Interpret the durable JSON event protocol emitted by AFK agent adapters."""

import json
import math
import os
from pathlib import Path

# Pi defaults to three model retries. Keep interpretation bounded even when an
# untrusted event stream advertises a larger maximum.
MAX_AUTO_RETRIES = 3


def read_only_pi_command(
    configuration_name: str,
    system_prompt: str,
    model: str = "gpt-5.6-sol",
    thinking: str = "medium",
) -> list[str]:
    return pi_command(
        configuration_name, system_prompt, "read,grep,find,ls", model, thinking
    )


def write_pi_command(
    configuration_name: str,
    system_prompt: str,
    model: str = "gpt-5.6-sol",
    thinking: str = "medium",
) -> list[str]:
    return pi_command(
        configuration_name,
        system_prompt,
        "read,bash,edit,write,grep,find,ls",
        model,
        thinking,
    )


def no_tool_pi_command(
    configuration_name: str, system_prompt: str, model: str, thinking: str
) -> list[str]:
    return pi_command(configuration_name, system_prompt, None, model, thinking)


def pi_command(
    configuration_name: str,
    system_prompt: str,
    tools: str | None,
    model: str = "gpt-5.6-sol",
    thinking: str = "medium",
) -> list[str]:
    configured = os.environ.get(configuration_name)
    if configured is not None:
        command = json.loads(configured)
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) for argument in command)
        ):
            raise ValueError(f"{configuration_name} must be a JSON argv array")
        return command
    return [
        "/usr/bin/env",
        "PI_TELEMETRY=0",
        "PI_SKIP_VERSION_CHECK=1",
        "pi",
        "--provider",
        "openai-codex",
        "--model",
        model,
        "--thinking",
        thinking,
        "--mode",
        "json",
        "--print",
        "--no-session",
        *(["--tools", tools] if tools is not None else ["--no-tools"]),
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--system-prompt",
        system_prompt,
    ]


def agent_response(events_path: Path) -> dict[str, object]:
    state = "segment"
    saw_final_end = False
    saw_settled = False
    retry_count = 0
    retry_completed = False
    terminal_message = None
    try:
        lines = events_path.read_bytes().decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return error("invalid agent event encoding")
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return error("invalid agent event JSON")
        if not isinstance(event, dict):
            return error("invalid agent event JSON")
        event_type = event.get("type")

        if saw_final_end:
            if event_type != "agent_settled" or saw_settled:
                return error("events follow agent_end")
            saw_settled = True
            continue
        if event_type == "agent_settled":
            return error("agent_settled precedes agent_end")

        if state == "retry_start":
            if event_type != "auto_retry_start" or not valid_retry_start(
                event, retry_count
            ):
                return error("invalid auto-retry event sequence")
            state = "agent_start"
            continue
        if state == "agent_start":
            if event_type != "agent_start":
                return error("invalid auto-retry event sequence")
            state = "segment"
            continue
        if event_type == "auto_retry_start":
            return error("invalid auto-retry event sequence")

        if event_type == "message_end":
            message = event.get("message")
            if not valid_message(message):
                return error("invalid agent event JSON")
            if message["role"] == "assistant":
                if retry_completed:
                    return error("conflicting terminal assistant messages")
                if message["stopReason"] != "toolUse":
                    if terminal_message is not None:
                        return error("conflicting terminal assistant messages")
                    terminal_message = message
            continue

        if event_type == "auto_retry_end":
            if (
                retry_count == 0
                or retry_completed
                or event.get("success") is not True
                or not valid_retry_attempt(event.get("attempt"), retry_count)
                or terminal_message is None
                or terminal_message.get("stopReason") in {"error", "aborted"}
            ):
                return error("invalid auto-retry event sequence")
            retry_completed = True
            continue

        if event_type == "agent_end":
            if "willRetry" in event and not isinstance(event["willRetry"], bool):
                return error("invalid agent event JSON")
            if event.get("willRetry") is True:
                if (
                    retry_completed
                    or terminal_message is None
                    or terminal_message.get("stopReason") != "error"
                    or retry_count >= MAX_AUTO_RETRIES
                ):
                    return error("invalid auto-retry event sequence")
                retry_count += 1
                terminal_message = None
                state = "retry_start"
                continue
            if retry_count and (
                event.get("willRetry") is not False or not retry_completed
            ):
                return error("invalid auto-retry event sequence")
            saw_final_end = True

    if (
        not saw_final_end
        or terminal_message is None
        or state != "segment"
        or (retry_count > 0 and not saw_settled)
    ):
        return error("agent event stream did not complete")
    if terminal_message.get("stopReason") == "error":
        return error(terminal_message.get("errorMessage", "agent error"))
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


def error(message: str) -> dict[str, object]:
    return {"agent": {"status": "error", "error": message}, "text": None}
