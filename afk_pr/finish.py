"""Explicit native merge and selected Bead closure; no completion judgment."""

import fcntl
import os
import re
import subprocess

from afk_pr import jobs
from afk_pr.beads import close_configured_bead, read_configured_bead
from afk_pr.config import load_config
from afk_pr.github import GitHub, identity
from afk_runtime import timestamp


class FinishGitHub:
    def __init__(self):
        self.github = GitHub()

    def observe(self, url):
        repo, number = identity(url)
        pr = self.github.api(f"repos/{repo}/pulls/{number}")
        return {
            "head": pr["head"]["sha"],
            "base": pr["base"]["ref"],
            "repository": pr["base"]["repo"]["full_name"].lower(),
            "merged": pr["merged"],
            "state": pr["state"],
            "merge_commit": pr.get("merge_commit_sha") if pr["merged"] else None,
            "associations": sorted(
                set(
                    re.findall(
                        r"<!-- afk-bead:([A-Za-z0-9][A-Za-z0-9._-]*) -->",
                        pr.get("body") or "",
                    )
                )
            ),
            "observed_at": timestamp(),
        }

    def merge(self, intent, log):
        # Native GitHub policy/queue handling, with no bypass or deletion flags.
        with log.open("w") as diagnostics:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "merge",
                    intent["pr_url"],
                    "--" + intent["method"],
                    "--match-head-commit",
                    intent["head"],
                ],
                stdout=diagnostics,
                stderr=diagnostics,
                timeout=120,
                check=False,
            )
        if result.returncode:
            raise RuntimeError("Native merge failed; inspect private diagnostics")


def matches(intent, observed):
    return all(intent[key] == observed[key] for key in ("head", "base", "repository"))


def request_merge(intent, github, directory, result):
    """Reobserve even after a failed request: the server may have merged it."""
    observed = github.observe(intent["pr_url"])
    result["observed"] = observed
    if not matches(intent, observed):
        result["merge"] = "changed"
        return
    if observed["merged"]:
        result["merge"] = "confirmed"
        return
    if observed["state"] != "open":
        result["merge"] = "closed_unmerged"
        return
    # Persist intent to attempt the external operation before invoking it.
    result["request"] = "started"
    jobs.write(directory / "result.json", result)
    try:
        github.merge(intent, directory / "merge.log")
        result["request"] = "succeeded"
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result["request"] = "failed_or_unknown"
        result["request_error"] = type(error).__name__
    observed = github.observe(intent["pr_url"])
    result["observed"] = observed
    result["merge"] = (
        "changed"
        if not matches(intent, observed)
        else "confirmed"
        if observed["merged"]
        else "not_merged"
        if result["request"] == "failed_or_unknown"
        else "pending"
    )


def close_after_merge(intent, github, config, directory, result, read_bead, close_bead):
    """Only the explicitly selected task can close, after fresh merge evidence."""
    bead_id = intent["close_bead"]
    if bead_id is None:
        result["closure"] = "not_requested"
        return
    result["closure"] = "unknown"
    observed = github.observe(intent["pr_url"])
    result["observed"] = observed
    if not matches(intent, observed) or not observed["merged"]:
        result["closure"] = "not_confirmed"
        return
    bead = read_bead(bead_id, config)
    if bead["status"] == "closed":
        result["closure"] = "already_closed"
        return
    result["closure"] = "started"
    jobs.write(directory / "result.json", result)
    try:
        close_bead(
            bead_id,
            config,
            f"Completed by merged PR {intent['pr_url']}",
            directory / "beads.log",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result["closure_error"] = type(error).__name__
    # A lost response can hide a successful close; current state resolves it.
    result["closure"] = "unknown"
    bead = read_bead(bead_id, config)
    result["closure"] = "closed" if bead["status"] == "closed" else "failed"


def finish(
    url,
    config_path,
    *,
    apply=None,
    close_bead=None,
    method=None,
    github=None,
    read_bead=read_configured_bead,
    close_task=close_configured_bead,
):
    from afk_run import PreparationError

    repo, _ = identity(url)
    config = load_config(config_path)
    github = github or FinishGitHub()
    root = config["run_root"] / "finishes"
    if apply is None:
        if method not in {None, "merge", "squash", "rebase"}:
            raise ValueError("unsupported merge method")
        observed = github.observe(url)
        if observed["repository"] != repo.lower():
            raise ValueError("PR target repository mismatch")
        bead = read_bead(close_bead, config) if close_bead else None
        intent = {
            "pr_url": url,
            "repository": repo.lower(),
            "head": observed["head"],
            "base": observed["base"],
            "method": method or "merge",
            "close_bead": close_bead,
        }
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = root / os.urandom(8).hex()
        directory.mkdir(mode=0o700)
        preview = {
            "id": directory.name,
            "state": "preview",
            "created_at": timestamp(),
            "intent": intent,
            "observed": observed,
            "bead": bead,
        }
        jobs.write(directory / "preview.json", preview)
        return preview
    if close_bead is not None or method is not None:
        raise ValueError(
            "create a new preview to change closure target or merge method"
        )
    if not re.fullmatch(r"[0-9a-f]{16}", apply):
        raise ValueError("invalid finish preview ID")
    directory = root / apply
    preview = jobs.read(directory / "preview.json")
    intent = preview["intent"]
    if identity(intent["pr_url"]) != identity(url):
        raise ValueError("preview belongs to another PR")
    # Preview files are local operator-owned configuration, not signed authority.
    if intent["method"] not in {"merge", "squash", "rebase"}:
        raise ValueError("invalid retained merge method")
    with (directory / "apply.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("this preview is already being applied") from error
        attempt = directory / os.urandom(8).hex()
        attempt.mkdir(mode=0o700)
        result = {
            "id": apply,
            "state": "incomplete",
            "started_at": timestamp(),
            "intent": intent,
            "directory": str(attempt),
            "merge": "unknown",
            "closure": "not_attempted",
        }
        jobs.write(attempt / "result.json", result)
        try:
            # Invalid/missing task must fail before requesting any merge.
            if intent["close_bead"]:
                read_bead(intent["close_bead"], config)
            request_merge(intent, github, attempt, result)
            if result["merge"] == "confirmed":
                close_after_merge(
                    intent, github, config, attempt, result, read_bead, close_task
                )
            if result["merge"] == "confirmed" and result["closure"] in {
                "not_requested",
                "already_closed",
                "closed",
            }:
                result["state"] = "completed"
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            PreparationError,
            subprocess.SubprocessError,
        ) as error:
            result["error"] = type(error).__name__
        except KeyboardInterrupt:
            result["error"] = "KeyboardInterrupt"
        result["finished_at"] = timestamp()
        jobs.write(attempt / "result.json", result)
        return result
