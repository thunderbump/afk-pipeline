"""Acquire isolated phase clones using recorded candidate identities."""

import os
import subprocess
from pathlib import Path


def acquire(directory, job, phase):
    from afk_pr import jobs
    from afk_pr.config import repository
    from afk_pr.github import identity

    repo = repository(job["repository"])
    if repo != job["github_repository"]:
        raise ValueError("recorded repository identity changed")
    root = Path(job["workspace_root"]) / job["id"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = root / phase
    if workspace.exists():
        raise ValueError("workspace already exists; inspect retained acquisition")
    timeout = job["acquisition_timeout"]

    def run(*args, required=True):
        command = [
            "git",
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            *args,
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        # Git errors may include host/private paths, so retain privately rather than post.
        with (directory / f"{phase}-acquisition.log").open("a") as log:
            log.write(f"git {args[0]}: exit {result.returncode}\n{result.stderr}\n")
        if required and result.returncode:
            raise RuntimeError(
                f"repository acquisition failed at git {args[0]}; inspect retained acquisition log and worker gh authentication"
            )
        return result

    run("clone", "--no-checkout", job["repository"], str(workspace))
    if job.get("pr_url"):
        _, number = identity(job["pr_url"])
        run(
            "-C",
            str(workspace),
            "fetch",
            "origin",
            f"refs/pull/{number}/head",
            required=False,
        )
    for sha in {job["head"], job["base"]}:
        if run(
            "-C", str(workspace), "cat-file", "-e", sha + "^{commit}", required=False
        ).returncode:
            run("-C", str(workspace), "fetch", "origin", sha, required=False)
        if run(
            "-C", str(workspace), "cat-file", "-e", sha + "^{commit}", required=False
        ).returncode:
            raise ValueError(f"pinned_commit_unavailable: {sha}")
    run("-C", str(workspace), "checkout", "--detach", job["head"])
    run("-C", str(workspace), "submodule", "update", "--init", "--recursive")
    if jobs.git(workspace, "rev-parse", "HEAD") != job["head"] or jobs.git(
        workspace, "status", "--porcelain"
    ):
        raise ValueError("acquired workspace does not match clean recorded candidate")
    return workspace
