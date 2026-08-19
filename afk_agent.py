"""Interpret the durable JSON event protocol emitted by AFK agent adapters."""

import json
import os
from pathlib import Path


def read_only_pi_command(configuration_name: str, system_prompt: str) -> list[str]:
    return pi_command(configuration_name, system_prompt, "read,grep,find,ls")


def write_pi_command(configuration_name: str, system_prompt: str) -> list[str]:
    return pi_command(
        configuration_name, system_prompt, "read,bash,edit,write,grep,find,ls"
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
    saw_end = False
    saw_settled = False
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
        if saw_end:
            if event.get("type") != "agent_settled" or saw_settled:
                return error("events follow agent_end")
            saw_settled = True
            continue
        if event.get("type") == "agent_settled":
            return error("agent_settled precedes agent_end")
        if event.get("type") == "message_end":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(
                message.get("role"), str
            ):
                return error("invalid agent event JSON")
            if message["role"] == "assistant":
                stop_reason = message.get("stopReason")
                content = message.get("content", [])
                if not isinstance(stop_reason, str) or not isinstance(content, list):
                    return error("invalid agent event JSON")
                for part in content:
                    if not isinstance(part, dict) or not isinstance(
                        part.get("type"), str
                    ):
                        return error("invalid agent event JSON")
                    if part["type"] == "text" and not isinstance(part.get("text"), str):
                        return error("invalid agent event JSON")
                if (
                    stop_reason == "error"
                    and "errorMessage" in message
                    and not isinstance(message["errorMessage"], str)
                ):
                    return error("invalid agent event JSON")
                terminal_message = message
        if event.get("type") == "agent_end":
            saw_end = True
    if not saw_end or terminal_message is None:
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


def error(message: str) -> dict[str, object]:
    return {"agent": {"status": "error", "error": message}, "text": None}
