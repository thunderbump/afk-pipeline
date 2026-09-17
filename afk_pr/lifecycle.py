"""Coordinate workers and explicit deletion of successful disposable clones."""

import fcntl
import functools
import json
import re
import shutil
import subprocess
from pathlib import Path


def guarded(function):
    @functools.wraps(function)
    def call(directory, *args, **kwargs):
        directory = Path(directory)
        with (directory / "lifecycle.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            marker = directory / "cleanup.json"
            if marker.exists() and (
                function.__name__ != "retry_publication"
                or json.loads(marker.read_text())["state"] != "removed"
            ):
                raise ValueError("workspace cleanup started; this job cannot restart")
            return function(directory, *args, **kwargs)

    return call


def quiescent(job_id, phase):
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            f"afk-pr-{job_id}-{phase}",
            "--property=LoadState",
            "--property=ActiveState",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if fields.get("LoadState") == "not-found" and result.returncode in {0, 4}:
        return True
    return result.returncode == 0 and fields.get("ActiveState") in {
        "inactive",
        "failed",
    }


def cleanup(directory, *, dry_run=False):
    from afk_pr import jobs

    directory = Path(directory)
    with (directory / "lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"outcome": "retained", "reason": "job is active"}
        job = jobs.read(directory / "job.json")
        if job.get("layout") != "independent-clones-v1":
            return {
                "outcome": "retained",
                "reason": "historical or unknown workspace layout",
            }
        if not job.get("cleanup_allowed"):
            return {
                "outcome": "retained",
                "reason": "fixture resource release is not proven; cleanup disabled",
            }
        phases = job.get("expected_phases", [])
        if (
            not phases
            or len(set(phases)) != len(phases)
            or set(phases) - set(jobs.PHASES)
        ):
            raise ValueError("missing or invalid expected phases")
        if not re.fullmatch(r"[0-9a-f]{16}", job["id"]) or directory.name != job["id"]:
            raise ValueError("invalid job ownership")
        root = Path(job["workspace_root"]) / job["id"]
        if (
            not root.is_absolute()
            or root.resolve() != root
            or directory.resolve().is_relative_to(root)
            or root.is_relative_to(directory.resolve())
        ):
            raise ValueError("unsafe workspace ownership path")
        marker = directory / "cleanup.json"
        previous = jobs.read(marker) if marker.exists() else None
        if previous and previous.get("state") == "removed":
            return {"outcome": "removed", "directories": previous["directories"]}
        targets = [root / phase for phase in phases]
        if previous:
            if previous.get("state") != "deleting" or previous.get("directories") != [
                str(p) for p in targets
            ]:
                raise ValueError("invalid retained cleanup decision")
        else:
            reason = eligibility(directory, job, phases, targets)
            if reason:
                return {"outcome": "retained", "reason": reason}
        # Recheck unit inactivity even when resuming an interrupted deletion.
        if not all(quiescent(job["id"], phase) for phase in phases):
            return {"outcome": "retained", "reason": "worker inactivity is unconfirmed"}
        for target in targets:
            if target.is_symlink() or (
                target.exists() and (target.resolve() != target or not target.is_dir())
            ):
                raise ValueError("unsafe phase workspace")
            if (target / ".git").exists() and not (target / ".git").is_dir():
                raise ValueError("linked worktrees are excluded from cleanup")
        record = {"state": "deleting", "directories": [str(p) for p in targets]}
        if dry_run:
            return {"outcome": "eligible", "directories": record["directories"]}
        jobs.write(marker, record)
        for target in targets:
            if target.exists():
                shutil.rmtree(target)
        record["state"] = "removed"
        jobs.write(marker, record)
        return {"outcome": "removed", "directories": record["directories"]}


def eligibility(directory, job, phases, targets):
    from afk_pr import jobs

    # Required evidence must remain self-contained, not point into a clone.
    if any(p.is_symlink() for p in directory.rglob("*")):
        return "job evidence contains symlinks; inspect retained artifacts"
    for phase, target in zip(phases, targets):
        path = directory / f"{phase}.json"
        if not path.is_file():
            return f"missing {phase} result"
        record = jobs.read(path)
        if (
            record.get("state") != ("passed" if phase == "fixtures" else "completed")
            or record.get("publication") != "published"
        ):
            return f"{phase} is not successful and published"
        required = (
            ["fixtures.stdout.log", "fixtures.stderr.log"]
            if phase == "fixtures"
            else [f"{phase}.md", "inference/receipt.json"]
        )
        required.append(
            "bead.json"
            if phase == "creation"
            else "context.json"
            if phase in {"review", "response"}
            else "job.json"
        )
        if not all((directory / name).is_file() for name in required):
            return f"{phase} required evidence is missing"
        head = job["head"]
        if phase in {"creation", "response"}:
            progress_path = directory / f"{phase}-progress.json"
            if not progress_path.exists():
                return "missing publication progress"
            progress = jobs.read(progress_path)
            head = progress["candidate"]
            changed = phase == "creation" or progress.get("changed")
            if changed:
                if progress.get("push") != "pushed":
                    return "candidate push is uncertain or unpublished"
                child_id = progress.get("fixture_job", "")
                if not re.fullmatch(r"[0-9a-f]{16}", child_id):
                    return "fixture handoff is missing"
                child = directory.parent / child_id
                if not (child / "fixtures.json").is_file():
                    return "fixture child result is missing"
                child_job = jobs.read(child / "job.json")
                child_result = jobs.read(child / "fixtures.json")
                if (
                    child_job.get(f"{phase}_job") != job["id"]
                    or child_job.get("head") != head
                    or child_result.get("state") != "passed"
                    or child_result.get("publication") != "published"
                ):
                    return "fixture child did not pass and publish for this candidate"
                if not quiescent(child_id, "fixtures"):
                    return "fixture child inactivity is unconfirmed"
        if (
            not target.is_dir()
            or target.is_symlink()
            or not (target / ".git").is_dir()
            or (target / ".git").is_symlink()
        ):
            return "workspace is missing or is not an owned independent clone"
        if jobs.git(target, "rev-parse", "HEAD") != head or jobs.git(
            target, "status", "--porcelain"
        ):
            return "workspace has unexpected changes or HEAD"
    return None
