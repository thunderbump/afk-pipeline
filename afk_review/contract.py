"""Validate structured Review results against the exact reviewed Git object."""

import re
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


def validate_context(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "work_base"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("work_base"), str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["work_base"]) is None
    ):
        raise ValueError("invalid Review work context")
    return value


def validate_input(value: object) -> dict[str, object]:
    """Validate the complete persisted Review input contract without I/O."""
    required = {
        "schema_version",
        "workspace",
        "change_directory",
        "validation_directory",
        "timeout_seconds",
    }
    allowed = required | {"related_work", "work_context", "review_mode"}
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("review must use schema_version 1")
    if "inference" in value:
        raise ValueError("review input cannot override inference policy")
    for field in ("workspace", "change_directory", "validation_directory"):
        path = value.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"review {field} must be an absolute path")
    if not set(value) <= allowed:
        raise ValueError("review input fields are malformed")
    if "work_context" in value:
        validate_context(value["work_context"])
    if value.get("review_mode", "combined") not in {"combined", "split"}:
        raise ValueError("review review_mode must be combined or split")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("review timeout_seconds must be a positive integer")
    return value


def validate_audit(value: object) -> dict[str, object]:
    """Validate the Review's declaration of the completed, ordered audit scopes."""
    if not isinstance(value, dict):
        raise TypeError("review audit must be an object")
    if set(value) != {"completed", "scopes"}:
        raise ValueError("review audit fields are malformed")
    if value["completed"] is not True or value["scopes"] != REVIEW_AUDIT["scopes"]:
        raise ValueError("review audit declaration is malformed")
    return value


def validate_review(
    value: object,
    workspace: Path,
    reviewed_head: str,
    related_work_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("review response must be an object")
    if set(value) != {"summary", "findings", "audit"}:
        raise ValueError("review response fields are malformed")
    if not isinstance(value.get("summary"), str):
        raise TypeError("review summary must be a string")
    if not value["summary"].strip():
        raise ValueError("review summary must be a non-empty string")
    validate_audit(value.get("audit"))
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise TypeError("review findings must be an array")
    for finding in findings:
        validate_finding(finding, workspace, reviewed_head, related_work_ids)
    return value


def validate_output_projection(
    output: object,
    workspace: Path,
    reviewed_head: str,
    related_work_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Validate an optional multi-invocation Review-to-aggregate mapping."""
    if not isinstance(output, dict):
        raise TypeError("Review output must be an object")
    aggregate = validate_review(
        output.get("review"), workspace, reviewed_head, related_work_ids
    )
    if "review_mode" not in output:
        return aggregate
    mode = output.get("review_mode")
    invocations = output.get("review_invocations")
    provenance = output.get("finding_provenance")
    expected_lenses = (
        ["combined"]
        if mode == "combined"
        else ["behavior", "design", "standards"]
        if mode == "split"
        else None
    )
    if (
        expected_lenses is None
        or not isinstance(invocations, list)
        or [item.get("lens") for item in invocations if isinstance(item, dict)]
        != expected_lenses
        or len(invocations) != len(expected_lenses)
        or not isinstance(provenance, list)
    ):
        raise ValueError("Review invocation projection is malformed")
    source_reviews = []
    for invocation, lens in zip(invocations, expected_lenses):
        expected_artifacts = (
            {
                "events": "events.jsonl",
                "stderr": "stderr.log",
                "inference": "inference",
            }
            if lens == "combined"
            else {
                "events": f"reviewers/{lens}/events.jsonl",
                "stderr": f"reviewers/{lens}/stderr.log",
                "inference": f"reviewers/{lens}/inference",
            }
        )
        allowed = {"lens", "outcome", "process", "agent", "review", "artifacts"}
        if (
            not isinstance(invocation, dict)
            or set(invocation) != allowed
            or invocation.get("outcome") != "succeeded"
            or invocation.get("agent") != {"status": "completed"}
            or not isinstance(invocation.get("process"), dict)
            or invocation.get("artifacts") != expected_artifacts
        ):
            raise ValueError("Review invocation projection is incomplete")
        raw = validate_review(
            invocation.get("review"), workspace, reviewed_head, related_work_ids
        )
        if lens != "combined" and any(
            finding["lens"] != lens for finding in raw["findings"]
        ):
            raise ValueError("Review invocation lens is malformed")
        source_reviews.append(raw)
    expected_findings = [
        finding for raw in source_reviews for finding in raw["findings"]
    ]
    expected_provenance = []
    finding_index = 0
    for lens, raw in zip(expected_lenses, source_reviews):
        for source_index, _finding in enumerate(raw["findings"]):
            expected_provenance.append(
                {
                    "finding_index": finding_index,
                    "lens": (
                        raw["findings"][source_index]["lens"]
                        if lens == "combined"
                        else lens
                    ),
                    "source_finding_index": source_index,
                }
            )
            finding_index += 1
    expected_summary = (
        source_reviews[0]["summary"]
        if mode == "combined"
        else "\n".join(
            f"{lens.capitalize()}: {raw['summary']}"
            for lens, raw in zip(expected_lenses, source_reviews)
        )
    )
    if (
        aggregate["findings"] != expected_findings
        or aggregate["summary"] != expected_summary
        or aggregate["audit"] != REVIEW_AUDIT
        or provenance != expected_provenance
    ):
        raise ValueError("Review aggregate provenance disagrees")
    return aggregate


def validate_finding(
    finding: object,
    workspace: Path,
    reviewed_head: str,
    related_work_ids: set[str] | frozenset[str] = frozenset(),
) -> None:
    if not isinstance(finding, dict):
        raise TypeError("each finding must be an object")
    if set(finding) != {"lens", "title", "details", "locations", "scope_claim"}:
        raise ValueError("finding fields are malformed")
    if finding.get("lens") not in {"behavior", "design", "standards"}:
        raise ValueError("finding lens must be behavior, design, or standards")
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
        if not isinstance(location, dict):
            raise TypeError("each finding location must be an object")
        if set(location) != {"path", "line"}:
            raise ValueError("finding location fields are malformed")
        if not isinstance(location.get("path"), str):
            raise TypeError("each finding location needs a path")
        if not location["path"].strip() or location["path"].startswith("/"):
            raise ValueError("finding location path must be repository-relative")
        line = location.get("line")
        if not isinstance(line, int) or isinstance(line, bool):
            raise TypeError("finding location line must be an integer")
        if line < 1:
            raise ValueError("finding location line must be a positive integer")
        validate_location(workspace, reviewed_head, location["path"], line)
    validate_scope_claim(finding.get("scope_claim"), related_work_ids)


def validate_scope_claim(
    value: object, related_work_ids: set[str] | frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("finding scope_claim must be an object")
    kind = value.get("kind")
    expected_fields = (
        {"kind", "rationale", "related_work_id"}
        if kind == "related"
        else {"kind", "rationale"}
    )
    if set(value) != expected_fields:
        raise ValueError("finding scope_claim fields are malformed")
    if kind not in {"current", "related", "unknown"}:
        raise ValueError(
            "finding scope_claim kind must be current, related, or unknown"
        )
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("finding scope_claim rationale must be a non-empty string")
    if kind == "related":
        related_id = value.get("related_work_id")
        if not isinstance(related_id, str) or related_id not in related_work_ids:
            raise ValueError(
                "finding scope_claim related_work_id must exist in related work"
            )
    return value


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
