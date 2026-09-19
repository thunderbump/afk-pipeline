"""Validated review findings and their retained, commit-bound context."""

import json
from pathlib import Path

from afk_inference.runtime import ResponseRejected
from afk_pr.github import identity


def validate(value):
    """Accept a JSON object or its textual encoding; never infer findings from prose."""
    if isinstance(value, str):
        if len(value) > 50000:
            raise ResponseRejected("review exceeds 50000 characters")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ResponseRejected("review must be a JSON object") from error
    if not isinstance(value, dict) or set(value) != {"summary", "findings"}:
        raise ResponseRejected("review requires summary and findings")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ResponseRejected("review summary must be nonempty text")
    if not isinstance(value["findings"], list):
        raise ResponseRejected("review findings must be a list")
    for finding in value["findings"]:
        if (
            not isinstance(finding, dict)
            or "message" not in finding
            or set(finding) - {"message", "path", "line"}
            or not isinstance(finding["message"], str)
            or not finding["message"].strip()
        ):
            raise ResponseRejected("each finding requires a nonempty message")
        if "path" in finding and (
            not isinstance(finding["path"], str) or not finding["path"].strip()
        ):
            raise ResponseRejected("finding path must be nonempty text when supplied")
        if "line" in finding and (
            "path" not in finding
            or type(finding["line"]) is not int
            or finding["line"] < 1
        ):
            raise ResponseRejected("finding line requires a path and positive integer")
    if len(json.dumps(value, ensure_ascii=False)) > 50000:
        raise ResponseRejected("review exceeds 50000 characters")
    return value


def render(report):
    """Keep the GitHub review readable while the phase record retains the structure."""
    sections = [report["summary"], "## Findings"]
    for finding in report["findings"]:
        location = finding.get("path", "")
        if "line" in finding:
            location += f":{finding['line']}"
        sections.append(
            "- " + (f"`{location}`: " if location else "") + finding["message"]
        )
    if not report["findings"]:
        sections.append("No actionable findings found.")
    return "\n\n".join(sections) + "\n"


def retained(run_root, url, head):
    """Freeze completed local reviews; legacy/external feedback stays in PR context."""
    repository, number = identity(url)
    results = []
    for path in sorted((Path(run_root) / "pr-reviews").glob("*/review.json")):
        job = json.loads((path.parent / "job.json").read_text())
        job_repository, job_number = identity(job["pr_url"])
        if (job_repository.lower(), job_number) != (repository.lower(), number):
            continue
        record = json.loads(path.read_text())
        if record.get("state") != "completed" or "result" not in record:
            continue
        report = record["result"]
        if report.get("schema_version") != 1 or report.get("head") != job["head"]:
            raise ResponseRejected("retained review result does not match its job")
        validate({"summary": report["summary"], "findings": report["findings"]})
        results.append(
            {
                "job_id": job["id"],
                "reviewer": record["reviewer"],
                "publication": record.get("publication", "pending"),
                "url": record.get("url"),
                "current_head": report["head"] == head,
                "result": report,
            }
        )
    return results
