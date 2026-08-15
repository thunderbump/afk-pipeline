"""Interpret the durable JSON event protocol emitted by AFK agent adapters."""

import json
from pathlib import Path


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
            if not isinstance(message, dict):
                return error("invalid agent event JSON")
            if message.get("role") == "assistant":
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
