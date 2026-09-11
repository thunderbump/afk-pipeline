"""Validate structured Review results against the exact reviewed Git object."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

_MAX_INVOCATION_BYTES = 1024 * 1024

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


def _validate_split_invocation_lens(
    receipt: dict[str, object],
    inference_directory: Path,
    lens: str,
    reader=None,
) -> None:
    """Bind a projected split lens to the receipt-authenticated task."""
    invocation_path = inference_directory / "invocation.json"
    raw = (
        reader.bytes(invocation_path, _MAX_INVOCATION_BYTES)
        if reader is not None
        else invocation_path.read_bytes()
    )
    if len(raw) > _MAX_INVOCATION_BYTES:
        raise ValueError("Review invocation lens disagrees with authenticated task")
    try:
        invocation = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "Review invocation lens disagrees with authenticated task"
        ) from error
    hashes = receipt.get("hashes")
    invocation_hash = (
        hashes.get("invocation_sha256") if isinstance(hashes, dict) else None
    )
    prompt = invocation.get("prompt") if isinstance(invocation, dict) else None
    version = (
        invocation.get("task_contract_version")
        if isinstance(invocation, dict)
        else None
    )
    marker = (
        f"This is the isolated {lens} lens invocation. Report only findings "
        f'with lens "{lens}".'
    )
    if (
        not isinstance(invocation_hash, str)
        or hashlib.sha256(raw).hexdigest() != invocation_hash
        or not isinstance(invocation, dict)
        or invocation.get("purpose") != "review"
        or version not in {8, 9}
        or invocation.get("requested_capability") != "READ_ONLY"
        or not isinstance(prompt, dict)
        or prompt.get("purpose") != "review"
        or prompt.get("task_contract_version") != version
        or not isinstance(prompt.get("trusted_task_instructions"), str)
        or not prompt["trusted_task_instructions"].endswith(marker)
    ):
        raise ValueError("Review invocation lens disagrees with authenticated task")


def validate_invocation_receipts(
    output: dict[str, object],
    review_directory: Path,
    reader=None,
) -> None:
    """Bind every started invocation projection to its retained receipt.

    Review's task contract accepts JSON text and projects the decoded object.
    A failed split legitimately contains only the started lens prefix; absent
    later lenses remain expected-but-unstarted evidence rather than malformed
    invocation records.
    """
    mode = output.get("review_mode", "combined")
    if mode not in {"combined", "split"}:
        return
    expected_lenses = (
        ["combined"] if mode == "combined" else ["behavior", "design", "standards"]
    )
    if "review_mode" not in output:
        # Before invocation projections were added, a combined Review's root
        # receipt was still the authority for the copied aggregate.
        receipt_path = review_directory / "inference" / "receipt.json"
        receipt = (
            reader.json(receipt_path)
            if reader is not None
            else json.loads(receipt_path.read_text())
        )
        terminal = (
            receipt.get("terminal_response") if isinstance(receipt, dict) else None
        )
        try:
            decoded_terminal = (
                json.loads(terminal) if isinstance(terminal, str) else None
            )
        except json.JSONDecodeError as error:
            raise ValueError(
                "Review invocation receipt disagrees with projection"
            ) from error
        if (
            output.get("outcome") != "completed"
            or not isinstance(receipt, dict)
            or receipt.get("outcome") != "succeeded"
            or not isinstance(receipt.get("protocol"), dict)
            or receipt["protocol"].get("status") != "accepted"
            or decoded_terminal != output.get("review")
        ):
            raise ValueError("Review invocation receipt disagrees with projection")
        return
    invocations = output.get("review_invocations")
    complete = output.get("outcome") == "completed"
    if (
        not isinstance(invocations, list)
        or not invocations
        or len(invocations) > len(expected_lenses)
        or (complete and len(invocations) != len(expected_lenses))
    ):
        raise ValueError("Review invocation projection is malformed")
    unsuccessful_seen = False
    for invocation, lens in zip(invocations, expected_lenses):
        if (
            not isinstance(invocation, dict)
            or invocation.get("lens") != lens
            or unsuccessful_seen
        ):
            raise ValueError("Review invocation projection is malformed")
        relative = (
            Path("inference")
            if lens == "combined"
            else Path("reviewers") / lens / "inference"
        )
        receipt_path = review_directory / relative / "receipt.json"
        receipt = (
            reader.json(receipt_path)
            if reader is not None
            else json.loads(receipt_path.read_text())
        )
        if mode == "split" and isinstance(receipt, dict):
            _validate_split_invocation_lens(receipt, receipt_path.parent, lens, reader)
        succeeded = invocation.get("outcome") == "succeeded"
        unsuccessful_seen = not succeeded
        terminal = (
            receipt.get("terminal_response") if isinstance(receipt, dict) else None
        )
        try:
            decoded_terminal = (
                json.loads(terminal) if isinstance(terminal, str) else None
            )
        except json.JSONDecodeError as error:
            raise ValueError(
                "Review invocation receipt disagrees with projection"
            ) from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("outcome") != invocation.get("outcome")
            or (
                succeeded
                and (
                    not isinstance(receipt.get("protocol"), dict)
                    or receipt["protocol"].get("status") != "accepted"
                    or decoded_terminal != invocation.get("review")
                )
            )
            or (not succeeded and invocation.get("review") is not None)
        ):
            raise ValueError("Review invocation receipt disagrees with projection")


def validate_output_projection(
    output: object,
    workspace: Path,
    reviewed_head: str,
    related_work_ids: set[str] | frozenset[str] = frozenset(),
    review_directory: Path | None = None,
    reader=None,
) -> dict[str, object]:
    """Validate an optional multi-invocation Review-to-aggregate mapping.

    When retained evidence is being consumed, ``review_directory`` binds each
    projected raw Review to the accepted terminal response in its invocation
    receipt. The receipt, rather than the mutable copy in output.json, is the
    authoritative per-invocation result.
    """
    if not isinstance(output, dict):
        raise TypeError("Review output must be an object")
    aggregate = validate_review(
        output.get("review"), workspace, reviewed_head, related_work_ids
    )
    if "review_mode" not in output:
        if review_directory is not None:
            receipt_path = review_directory / "inference" / "receipt.json"
            if receipt_path.is_file():
                receipt = (
                    reader.json(receipt_path)
                    if reader is not None
                    else json.loads(receipt_path.read_text())
                )
                # Pre-projection Review receipts use JSON text as the terminal
                # response. Older synthetic/non-Review evidence can have no
                # receipt or an unrelated structured terminal value.
                if isinstance(receipt.get("terminal_response"), str):
                    validate_invocation_receipts(output, review_directory, reader)
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
    if review_directory is not None:
        validate_invocation_receipts(output, review_directory, reader)
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
