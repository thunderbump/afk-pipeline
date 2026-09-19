"""Read an optional repository-authored public fixture summary, never nested logs."""

import html
import json
import re
from pathlib import Path

FIELDS = {
    "schema_version",
    "head",
    "profile",
    "status",
    "step",
    "diagnostic_codes",
    "timings_ms",
}


def validate(value, head):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("invalid public fixture summary fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unknown public fixture summary version")
    if value["head"] != head:
        raise ValueError("summary belongs to another candidate")
    if value["status"] not in ("passed", "failed"):
        raise ValueError("invalid repository outcome")
    codes = value["diagnostic_codes"]
    if not isinstance(codes, list) or len(codes) > 24:
        raise ValueError("invalid diagnostic codes")
    tokens = [value["profile"], *codes]
    if value["step"] is not None:
        tokens.append(value["step"])
    if any(
        not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", item)
        for item in tokens
    ):
        raise ValueError("diagnostics must be bounded identifiers, not free text")
    timings = value["timings_ms"]
    if not isinstance(timings, dict) or set(timings) != {"validation", "restore"}:
        raise ValueError("invalid timing fields")
    if any(
        item is not None and (type(item) is not int or not 0 <= item <= 604800000)
        for item in timings.values()
    ):
        raise ValueError("invalid elapsed milliseconds")
    return value


def public_summary(directory, job):
    """Invalid/absent summaries fall back to the existing bounded log publication."""
    from afk_export import ExportError, sanitize_public_artifact_text

    path = directory / "fixture-evidence/public-summary.json"
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(directory.resolve())
        ):
            return ""
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return ""
        value = validate(json.loads(raw), job["head"])
        rendered = sanitize_public_artifact_text(
            json.dumps(value, indent=2),
            [job["repository"], str(directory), str(Path.home())],
        )
    except (OSError, ValueError, TypeError, RecursionError, ExportError):
        return ""
    return (
        "\n\nRepository diagnostic summary (reported by the repository; the AFK result above is authoritative):"
        "\n<pre>" + html.escape(rendered).replace("@", "@\u200b") + "</pre>"
    )
