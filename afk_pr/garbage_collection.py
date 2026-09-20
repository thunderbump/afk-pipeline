"""Explicit retention of PR jobs; resource adapters hold leases through deletion."""

import fcntl
import importlib.util
import re
import shutil
import subprocess
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from afk_pr import jobs, lifecycle


class Retain(ValueError):
    """A resource is not demonstrably disposable."""


def allocated(path):
    if not path.exists():
        return 0
    return sum(p.lstat().st_blocks * 512 for p in [path, *path.rglob("*")])


def owned_path(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise Retain("path is not a physical absolute directory")
    if path.exists() and not path.is_dir():
        raise Retain("target is not a directory")
    return path


def clean_clone(path, head):
    if not path.exists():
        return
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise Retain("linked worktree or incomplete clone")
    if jobs.git(path, "rev-parse", "HEAD") != head or jobs.git(
        path, "status", "--porcelain", "--untracked-files=all"
    ):
        raise Retain("workspace has unexpected HEAD or unpublished changes")
    # Nested submodule changes are included by status; an ignored build is disposable.


def workspace_targets(directory, job):
    if job.get("layout") != "independent-clones-v1":
        raise Retain("historical or unknown workspace layout")
    phases = job.get("expected_phases", [])
    if not phases or len(set(phases)) != len(phases) or set(phases) - set(jobs.PHASES):
        raise Retain("missing or invalid expected phases")
    root = owned_path(Path(job["workspace_root"]) / job["id"])
    if root.is_relative_to(directory) or directory.is_relative_to(root):
        raise Retain("workspace and evidence overlap")
    for phase in phases:
        if not lifecycle.quiescent(job["id"], phase):
            raise Retain("worker inactivity is unconfirmed")
        record = jobs.read(directory / f"{phase}.json")
        terminal = (
            {"passed", "failed", "timed_out"} if phase == "fixtures" else {"completed"}
        )
        if (
            record.get("state") not in terminal
            or record.get("publication") != "published"
        ):
            raise Retain(f"{phase} is not terminal and published")
        required = (
            ["fixtures.stdout.log", "fixtures.stderr.log"]
            if phase == "fixtures"
            else [f"{phase}.md", "inference/receipt.json"]
        )
        if not all(
            (directory / name).is_file() and not (directory / name).is_symlink()
            for name in required
        ):
            raise Retain("required diagnostic evidence is missing")
        head = job["head"]
        if phase in {"creation", "response"}:
            progress = jobs.read(directory / f"{phase}-progress.json")
            head = progress["candidate"]
            if (phase == "creation" or progress.get("changed")) and progress.get(
                "push"
            ) != "pushed":
                raise Retain("candidate push is unproven")
        clean_clone(owned_path(root / phase), head)
    return [root / phase for phase in phases]


@contextmanager
def resource_targets(config, directory, job, apply):
    resource = job.get("fixture_resource")
    if not resource:
        if not job.get("cleanup_allowed"):
            raise Retain("cleanup disabled")
        yield []
        return
    registered = config.get("fixture_resources", {}).get(resource["name"], {})
    for key in ("worker_home", "stack_path"):
        if str(Path(registered.get(key, "")).expanduser().resolve()) != resource[key]:
            raise Retain("fixture resource registration changed")
    adapter = registered.get("cleanup_adapter")
    if not adapter:
        raise Retain("fixture resource has no release-checking cleanup adapter")
    # Only operator-owned host configuration selects executable code. Never load a
    # cleanup adapter from the candidate or from an untrusted job declaration.
    spec = importlib.util.spec_from_file_location("afk_resource_cleanup", adapter)
    if spec is None or spec.loader is None:
        raise Retain("cleanup adapter must be a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.cleanup_targets(directory, job, apply=apply) as targets:
        yield [owned_path(p) for p in targets]


def retained_jobs(entries, keep):
    groups = defaultdict(list)
    for directory, job in entries:
        groups[
            (job.get("project"), job.get("pr_url") or job.get("bead_id") or job["id"])
        ].append((directory, job))
    protected = set()
    for group in groups.values():
        group.sort(
            key=lambda item: (item[1].get("created_at", ""), item[1]["id"]),
            reverse=True,
        )
        protected.update(job["id"] for _, job in group[:keep])
        seen = set()
        for directory, job in group:
            kinds = []
            kind = job.get("kind")
            if kind in {"creation", "response"}:
                record = directory / f"{kind}.json"
                if record.is_file() and jobs.read(record).get("state") == "completed":
                    kinds.append("latest candidate workspace")
            if (directory / "fixtures.json").is_file():
                state = jobs.read(directory / "fixtures.json").get("state")
                if state in {"passed", "failed", "timed_out"}:
                    kinds.append("success" if state == "passed" else "failure")
            for kind in kinds:
                if kind not in seen:
                    protected.add(job["id"])
                    seen.add(kind)
    return protected


def collect_job(config, directory, job, apply):
    with (directory / "lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Retain("job is active")
        marker = directory / "cleanup.json"
        previous = jobs.read(marker) if marker.exists() else None
        if previous and previous.get("state") == "removed":
            return {
                "outcome": "removed",
                "bytes_removed": 0,
                "directories": previous["directories"],
            }
        if Path(job["workspace_root"]) != config["workspace_root"]:
            raise Retain("workspace root no longer matches host configuration")
        targets = workspace_targets(directory, job)
        with resource_targets(config, directory, job, apply) as extra:
            targets += extra
            # Adapters own external path validation; never permit deletion of the
            # durable job directory, or overlapping targets counted twice.
            for target in targets:
                owned_path(target)
                if directory.is_relative_to(target):
                    raise Retain("target contains durable job evidence")
                if any(
                    other != target
                    and (target.is_relative_to(other) or other.is_relative_to(target))
                    for other in targets
                ):
                    raise Retain("cleanup targets overlap")
            paths = [str(p) for p in targets]
            if len(set(paths)) != len(paths):
                raise Retain("duplicate cleanup targets")
            if previous and (
                previous.get("state") != "deleting"
                or previous.get("directories") != paths
            ):
                raise Retain("interrupted cleanup target list changed")
            size = sum(allocated(p) for p in targets)
            result = {
                "outcome": "eligible",
                "bytes_eligible": size,
                "directories": paths,
            }
            if not apply:
                return result
            record = {"state": "deleting", "directories": paths, "command": "gc"}
            jobs.write(marker, record)
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
            record["state"] = "removed"
            record["bytes_removed"] = size
            jobs.write(marker, record)
            return {**result, "outcome": "removed", "bytes_removed": size}


def retained_size(config, directory, job):
    """Measure only known roots; unknown external artifacts are not inventoried."""
    targets = [directory]
    if Path(job.get("workspace_root", "")) == config["workspace_root"]:
        targets.append(config["workspace_root"] / job["id"])
    resource = job.get("fixture_resource", {})
    registered = config.get("fixture_resources", {}).get(resource.get("name"), {})
    result = directory / "fixture-evidence/result.json"
    if (
        registered.get("worker_home") == resource.get("worker_home")
        and resource.get("worker_home")
        and result.is_file()
    ):
        checkout = Path(jobs.read(result).get("checkout_dir", ""))
        if checkout.parent == Path(resource["worker_home"]) / "checkouts":
            targets.append(checkout)
    return sum(allocated(owned_path(p)) for p in targets)


def collect(config, project, *, keep=2, apply=False):
    """Keep recent jobs and success/failure evidence per PR; inspect each deletion."""
    if type(keep) is not int or keep < 1:
        raise ValueError("keep must be at least one")
    if project not in config.get("projects", {}):
        raise ValueError("project is not registered")
    root = owned_path(config["run_root"] / "pr-reviews")
    entries, results = [], []
    for directory in sorted(root.glob("*")):
        if not re.fullmatch(r"[0-9a-f]{16}", directory.name):
            continue
        try:
            owned_path(directory)
            job = jobs.read(directory / "job.json")
            if job.get("project") != project:
                continue
            if job["id"] != directory.name:
                raise Retain("job identity mismatch")
            entries.append((directory, job))
        except (OSError, ValueError, KeyError) as error:
            results.append(
                {"job_id": directory.name, "outcome": "retained", "reason": str(error)}
            )
    protected = retained_jobs(entries, keep)
    for directory, job in entries:
        try:
            if job["id"] in protected:
                raise Retain(
                    "retention policy: recent job or latest candidate/success/failure"
                )
            result = collect_job(config, directory, job, apply)
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            result = {"outcome": "retained", "reason": str(error)}
        try:
            result["bytes_retained"] = retained_size(config, directory, job)
        except (OSError, ValueError, KeyError):
            result["bytes_retained"] = None
        results.append({"job_id": job["id"], **result})
    return {
        "schema_version": 1,
        "project": project,
        "apply": apply,
        "keep": keep,
        "bytes_eligible": sum(r.get("bytes_eligible", 0) for r in results),
        "bytes_removed": sum(r.get("bytes_removed", 0) for r in results),
        "bytes_retained": sum(r.get("bytes_retained") or 0 for r in results),
        "unmeasured_jobs": sum(r.get("bytes_retained") is None for r in results),
        "jobs": results,
    }
