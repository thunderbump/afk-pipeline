"""Validate the durable Committed Change record shared by AFK modules."""

from pathlib import Path


def validate_change_output(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Committed Change must use schema_version 1")
    if value.get("outcome") != "completed":
        raise ValueError("Committed Change must have completed")
    change = value.get("change")
    if not isinstance(change, dict):
        raise TypeError("Committed Change change must be an object")
    objective = change.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("Committed Change objective must be a non-empty string")
    workspace = change.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("Committed Change workspace must be an absolute path")
    repository = change.get("repository")
    if not isinstance(repository, dict):
        raise TypeError("Committed Change repository must be an object")
    before = validate_repository_state(repository.get("before"))
    after = validate_repository_state(repository.get("after"))
    if before["dirty"] or before["status"] or after["dirty"] or after["status"]:
        raise ValueError("Committed Change repository states must be clean")
    if before["head"] == after["head"]:
        raise ValueError("Committed Change repository heads must be distinct")
    source = change.get("source")
    if not isinstance(source, dict) or source.get("kind") not in {
        "attempt",
        "feedback_response",
    }:
        raise ValueError("Committed Change source is invalid")
    source_directory = source.get("directory")
    if (
        not isinstance(source_directory, str)
        or not Path(source_directory).is_absolute()
    ):
        raise ValueError("Committed Change source directory must be an absolute path")
    return change


def validate_repository_state(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Committed Change repository state must be an object")
    head = value.get("head")
    branch = value.get("branch")
    dirty = value.get("dirty")
    status = value.get("status")
    if not isinstance(head, str) or not head:
        raise ValueError("Committed Change repository head must be a non-empty string")
    if branch is not None and not isinstance(branch, str):
        raise TypeError("Committed Change repository branch must be a string or null")
    if not isinstance(dirty, bool):
        raise TypeError("Committed Change repository dirty must be a boolean")
    if not isinstance(status, list) or not all(
        isinstance(line, str) for line in status
    ):
        raise TypeError("Committed Change repository status must be a string array")
    return {"head": head, "branch": branch, "dirty": dirty, "status": status}
