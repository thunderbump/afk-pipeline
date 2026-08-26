"""Validate structured Review results against the exact reviewed Git object."""

import subprocess
from pathlib import Path

REVIEW_AUDIT = {
    "completed": True,
    "scopes": [
        "objective",
        "acceptance_criteria",
        "reviewed_diff",
        "supplied_evidence",
    ],
}


def validate_audit(value: object) -> dict[str, object]:
    """Validate the Review's ordered declaration of the completed audit scope."""
    if not isinstance(value, dict):
        raise TypeError("review audit must be an object")
    if list(value) != ["completed", "scopes"]:
        raise ValueError("review audit fields must be completed then scopes")
    if value["completed"] is not True or value["scopes"] != REVIEW_AUDIT["scopes"]:
        raise ValueError("review audit declaration is malformed")
    return value


def validate_review(
    value: object, workspace: Path, reviewed_head: str
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("review response must be an object")
    if not isinstance(value.get("summary"), str):
        raise TypeError("review summary must be a string")
    validate_audit(value.get("audit"))
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise TypeError("review findings must be an array")
    for finding in findings:
        validate_finding(finding, workspace, reviewed_head)
    return value


def validate_finding(finding: object, workspace: Path, reviewed_head: str) -> None:
    if not isinstance(finding, dict):
        raise TypeError("each finding must be an object")
    if finding.get("severity") not in {"high", "medium", "low"}:
        raise ValueError("finding severity must be high, medium, or low")
    for field in ("title", "details"):
        if not isinstance(finding.get(field), str):
            raise TypeError(f"finding {field} must be a string")
        if not finding[field].strip():
            raise ValueError(f"finding {field} must be a non-empty string")
    locations = finding.get("locations")
    if not isinstance(locations, list):
        raise TypeError("finding locations must be an array")
    if not locations:
        raise ValueError("finding locations must not be empty")
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("path"), str):
            raise TypeError("each finding location needs a path")
        if not location["path"].strip() or location["path"].startswith("/"):
            raise ValueError("finding location path must be repository-relative")
        line = location.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            raise TypeError("finding location line must be an integer")
        if line < 1:
            raise ValueError("finding location line must be a positive integer")
        validate_location(workspace, reviewed_head, location["path"], line)


def validate_location(
    workspace: Path, reviewed_head: str, path: str, line: int
) -> None:
    root = workspace.resolve()
    target = (workspace / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("finding location path escapes the repository") from error
    entry = subprocess.run(
        ["git", "--literal-pathspecs", "ls-tree", "-z", reviewed_head, "--", path],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if entry.returncode != 0 or not entry.stdout:
        raise ValueError("finding location path must name a reviewed file")
    metadata, entry_path = entry.stdout.rstrip(b"\0").split(b"\t", 1)
    mode, object_type, object_id = metadata.split(b" ", 2)
    if (
        entry_path.decode("utf-8") != path
        or object_type != b"blob"
        or mode not in {b"100644", b"100755"}
    ):
        raise ValueError("finding location path must name a reviewed file")
    blob = subprocess.run(
        ["git", "cat-file", "blob", object_id.decode("ascii")],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0:
        raise ValueError("finding location path must name a reviewed file")
    try:
        line_count = len(blob.stdout.decode("utf-8").splitlines())
    except UnicodeDecodeError as error:
        raise ValueError("finding location path must name a text file") from error
    if line > line_count:
        raise ValueError("finding location line must exist in the reviewed file")
