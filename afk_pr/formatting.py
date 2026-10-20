"""Call the pinned formatter client before creation and response publication.

The trusted client owns snapshot scope, result validation and safe application.
AFK owns its selection, phase budget, private evidence and publication boundary.
"""

import json
import re
from pathlib import Path

from afk_runtime import run_command

QUERY_TIMEOUT = 30
JSON_LIMIT = 256 * 1024
LOG_LIMIT = 2 * 1024**2


def json_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > JSON_LIMIT:
        raise ValueError(
            "Formatter JSON is missing, linked or exceeds its output limit"
        )

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate formatter JSON key")
            value[key] = item
        return value

    value = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise TypeError("Formatter JSON must be an object")
    return value


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)


def normalize(directory, job, workspace, phase):
    """Stop before commit on any failed or unattributable formatter operation."""
    from afk_pr import jobs
    from afk_pr.config import formatter_policy

    if "formatter" not in job:
        return None
    evidence = directory / f"{phase}-formatting"
    evidence.mkdir(mode=0o700)
    try:
        policy = formatter_policy(job["formatter"])
        workspace = Path(workspace).absolute()
        if workspace.resolve() != workspace or Path(
            policy["command"][0]
        ).resolve().is_relative_to(workspace):
            raise ValueError(
                "Formatter executable or checkout traverses the candidate workspace"
            )

        def execute(name, args, timeout=QUERY_TIMEOUT):
            stdout, stderr = (
                evidence / f"{name}.stdout.json",
                evidence / f"{name}.stderr.log",
            )
            process = run_command(
                [*policy["command"], *args],
                evidence,
                timeout,
                stdout,
                stderr,
                termination_grace_seconds=policy["termination_grace_seconds"],
            )
            jobs.write(evidence / f"{name}.process.json", process)
            for path in (stdout, stderr):
                path.chmod(0o600)
                if path.stat().st_size > LOG_LIMIT:
                    with path.open("r+b") as stream:
                        stream.truncate(LOG_LIMIT)
                    raise ValueError(
                        "Formatter command exceeded its private output limit"
                    )
            if (
                process["exit_code"] != 0
                or process["error"]
                or process["timed_out"]
                or process["interrupted"]
            ):
                raise ValueError(
                    "Formatter command failed, timed out or was interrupted"
                )
            return stdout

        tool = json_file(execute("identity", ["--identity"]))
        if (
            type(tool.get("version")) is not int
            or tool["version"] != 1
            or tool.get("scope") != "working-snapshot-tool"
            or any(
                tool.get(k) != policy[k]
                for k in ("recipe_sha256", "implementation_sha256")
            )
        ):
            raise ValueError(
                "Formatter identity differs from the selected trusted recipe"
            )
        arguments = ["--repo", str(workspace), "--base", job["base"]]
        before = json_file(execute("input", [*arguments, "--snapshot-identity"]))
        if (
            type(before.get("version")) is not int
            or before["version"] != 1
            or before.get("scope") != "working-snapshot-input"
            or before.get("base") != job["base"]
            or before.get("head") != job["head"]
            or before.get("recipe_sha256") != policy["recipe_sha256"]
            or not sha256(before.get("input_snapshot_sha256"))
        ):
            raise ValueError("Formatter input identity does not match this candidate")
        result = evidence / "result.json"
        execute(
            "format", [*arguments, "--output", str(result)], policy["timeout_seconds"]
        )
        receipt_path = evidence / "apply-receipt.json"
        execute(
            "apply",
            [
                *arguments,
                "--apply-result",
                str(result),
                "--expected-input",
                before["input_snapshot_sha256"],
                "--expected-recipe",
                policy["recipe_sha256"],
                "--expected-implementation",
                policy["implementation_sha256"],
                "--output",
                str(receipt_path),
            ],
        )
        receipt = json_file(receipt_path)
        if (
            type(receipt.get("version")) is not int
            or receipt["version"] != 1
            or receipt.get("scope") != "working-snapshot-apply"
            or type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != 0
            or receipt.get("vm_started") is not False
            or any(
                receipt.get(k) != before[k]
                for k in ("base", "head", "input_snapshot_sha256", "recipe_sha256")
            )
            or receipt.get("implementation_sha256") != policy["implementation_sha256"]
            or not sha256(receipt.get("output_snapshot_sha256"))
            or not isinstance(receipt.get("applied_paths"), list)
            or len(receipt["applied_paths"]) > 128
            or not all(
                isinstance(path, str) and len(path) <= 240
                for path in receipt["applied_paths"]
            )
        ):
            raise ValueError(
                "Formatter application did not return a matching successful receipt"
            )
        jobs.write(
            evidence / "application.json",
            {
                "state": "completed",
                "input_snapshot_sha256": before["input_snapshot_sha256"],
                "output_snapshot_sha256": receipt["output_snapshot_sha256"],
                "changed_files": receipt["applied_paths"],
                "scope": "working-snapshot-format",
                "acceptance": False,
            },
        )
        return None
    except (OSError, ValueError, TypeError, KeyError) as error:
        jobs.write(
            evidence / "failure.json", {"error": str(error)[:2000], "acceptance": False}
        )
        return {
            "state": "failed",
            "reason": f"Pre-commit formatting failed; inspect private {phase}-formatting evidence. Editable work retained; no candidate committed or pushed.",
        }
