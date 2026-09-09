"""Validate the durable Assignment input shared by AFK components."""

import re
from pathlib import Path

from afk_related_work import validate_reference


def validate_assignment(assignment: object) -> dict[str, object]:
    if not isinstance(assignment, dict) or assignment.get("schema_version") != 1:
        raise ValueError("assignment must use schema_version 1")
    if (
        not isinstance(assignment.get("objective"), str)
        or not assignment["objective"].strip()
    ):
        raise ValueError("assignment objective must be a non-empty string")
    workspace = assignment.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("assignment workspace must be an absolute path")
    if "worker" in assignment:
        if assignment["worker"] != "inference" or "command" in assignment:
            raise ValueError(
                "assignment requires worker=inference or command, never both"
            )
    else:
        command = assignment.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
        ):
            raise ValueError("assignment command must be a non-empty argv string array")
    timeout = assignment.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("assignment timeout_seconds must be a positive integer")
    if "work_base" in assignment and (
        not isinstance(assignment["work_base"], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", assignment["work_base"]) is None
    ):
        raise ValueError("Assignment work_base must be a canonical commit ID")
    if "related_work" in assignment:
        validate_reference(assignment["related_work"])
        instructions = assignment.get("related_work_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("assignment related-work instructions are required")
    elif "related_work_instructions" in assignment:
        raise ValueError("assignment related-work instructions lack a reference")
    return assignment
