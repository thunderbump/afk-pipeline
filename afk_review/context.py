"""Bound complete-work scope and one repair cycle to existing source evidence."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from afk_evidence.access import MAX_JSON_BYTES, EvidenceReader
from afk_review.contract import validate_context
from afk_runtime import git

MAX_DIFF_BYTES = 16 * 1024 * 1024
CONTEXT_FILES = {
    "work_diff": "diff.patch",
    "repair_diff": "repair.patch",
    "previous_review": "previous-review.json",
    "previous_assessment": "previous-assessment.json",
    "previous_response": "previous-response.json",
}


def context_reader(input_path, change_directory):
    """Use the same caller-owned evidence roots as Committed Change."""
    roots = (Path(input_path).absolute().parent, Path(change_directory).absolute())
    configured = os.environ.get("AFK_STAGE_EVIDENCE_ROOTS")
    if configured is not None:
        roots = json.loads(configured)
        if not isinstance(roots, list) or not all(
            isinstance(root, str) and Path(root).is_absolute() for root in roots
        ):
            raise ValueError("invalid configured stage evidence roots")
    return EvidenceReader(roots)


def load_context(review_input, evidence, reader):
    # Local import keeps pure Review input validation independent of lineage.
    from afk_evidence.stages import verify_change_lineage

    requested = validate_context(review_input["work_context"])
    lineage = verify_change_lineage(
        Path(review_input["change_directory"]), reader=reader
    )
    change = evidence["change"]
    if (
        lineage.assignment.get("work_base") != requested["work_base"]
        or lineage.assignment["objective"] != change["objective"]
        or Path(lineage.assignment["workspace"]).absolute()
        != Path(review_input["workspace"]).absolute()
    ):
        raise ValueError("Review work base is not bound to its frozen Assignment")
    workspace = Path(review_input["workspace"])
    base = requested["work_base"]
    head = change["repository"]["after"]["head"]
    git(workspace, "merge-base", "--is-ancestor", base, head)
    return previous_cycle(change, reader)


def previous_cycle(change, reader):
    # Select one cycle from the already verified source chain. Validation-only
    # repairs carry no Assessment; walk past them to the nearest assessed repair.
    source = change["source"]
    visited = set()
    while source["kind"] != "attempt":
        if source["directory"] in visited:
            raise ValueError("Review previous-cycle evidence contains a cycle")
        visited.add(source["directory"])
        directory = Path(source["directory"])
        response_input = reader.json(directory / "input.json")
        if "assessment_directory" in response_input:
            assessment = Path(response_input["assessment_directory"])
            assessment_input = reader.json(assessment / "input.json")
            review = Path(assessment_input["review_directory"])
            return {
                "previous_review": reader.json(review / "output.json"),
                "previous_assessment": reader.json(assessment / "output.json"),
                "previous_response": reader.json(directory / "output.json"),
            }
        source = response_input["source"]
    return {}


def diff_bytes(workspace, before, after):
    """Bound captured Git output, rejecting an oversized patch rather than truncating."""
    with subprocess.Popen(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            f"{before}..{after}",
            "--",
        ],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        raw = process.stdout.read(MAX_DIFF_BYTES + 1)
        if len(raw) > MAX_DIFF_BYTES:
            process.kill()
            raise ValueError("Review diff exceeds 16 MiB")
        if process.wait() != 0:
            raise ValueError("Review Git diff failed")
    return raw


def write_context(directory, review_input, evidence, previous):
    """Materialize bounded caller-owned files, leaving the candidate untouched."""
    change = evidence["change"]
    workspace = Path(review_input["workspace"])
    base = review_input["work_context"]["work_base"]
    before = change["repository"]["before"]["head"]
    head = change["repository"]["after"]["head"]
    full = diff_bytes(workspace, base, head)
    payloads = {"work_diff": full}
    if base != before:
        payloads["repair_diff"] = diff_bytes(workspace, before, head)
    for key, value in previous.items():
        raw = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("previous Review cycle exceeds evidence limit")
        payloads[key] = raw
    files = {}
    for key, raw in payloads.items():
        name = CONTEXT_FILES[key]
        (directory / name).write_bytes(raw)
        files[key] = {
            "path": name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if "repair_diff" not in files:
        files["repair_diff"] = dict(files["work_diff"])
    return {
        **review_input["work_context"],
        "candidate": head,
        "repair_base": before,
        "files": files,
    }


def validate_artifacts(directory, requested, manifest, reader, change, repository=None):
    """Check retained context bytes before use by Review, Run readers or Export."""
    validate_context(requested)
    expected = {
        **requested,
        "candidate": change["repository"]["after"]["head"],
        "repair_base": change["repository"]["before"]["head"],
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {*expected, "files"}
        or any(manifest.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("Review context disagrees with its subject")
    files = manifest["files"]
    required = {"work_diff", "repair_diff"}
    previous = {"previous_review", "previous_assessment", "previous_response"}
    if not isinstance(files, dict) or set(files) not in (required, required | previous):
        raise ValueError("Review context file inventory is invalid")
    for key, item in files.items():
        name = (
            "diff.patch"
            if key == "repair_diff"
            and expected["repair_base"] == requested["work_base"]
            else CONTEXT_FILES[key]
        )
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "bytes", "sha256"}
            or item["path"] != name
            or type(item["bytes"]) is not int
        ):
            raise ValueError("Review context file reference is invalid")
        raw = reader.bytes(
            directory / name, MAX_DIFF_BYTES if key in required else MAX_JSON_BYTES
        )
        if (
            len(raw) != item["bytes"]
            or hashlib.sha256(raw).hexdigest() != item["sha256"]
        ):
            raise ValueError("Review context artifact hash disagrees")
        if repository is not None and key in required:
            base = (
                requested["work_base"]
                if key == "work_diff"
                else expected["repair_base"]
            )
            if raw != diff_bytes(repository, base, expected["candidate"]):
                raise ValueError("Review context diff disagrees with Git")
    return manifest


def validate_retained_context(
    directory, review_input, output, assignment, change, reader, repository=None
):
    """Bind a retained Review packet to its Assignment, Git range and nearest repair."""
    requested = (
        {"schema_version": 1, "work_base": assignment["work_base"]}
        if "work_base" in assignment
        else None
    )
    if review_input.get("work_context") != requested:
        raise ValueError("Review work context disagrees with Assignment")
    if requested is None:
        if "work_context" in output:
            raise ValueError("legacy Review has an unexpected work context")
        return
    manifest = validate_artifacts(
        directory, requested, output.get("work_context"), reader, change, repository
    )
    previous = previous_cycle(change, reader)
    files = manifest["files"]
    if set(files) - {"work_diff", "repair_diff"} != set(previous):
        raise ValueError("Review previous cycle inventory disagrees")
    for key, value in previous.items():
        if reader.json(directory / files[key]["path"]) != value:
            raise ValueError("Review previous cycle disagrees with source evidence")
