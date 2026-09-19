"""Local submission receipts; no workflow decisions or worker relaunches."""

import fcntl
import hashlib
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from afk_pr.github import identity
from afk_runtime import timestamp


def receipt_path(run_root, url, action_id):
    repository, number = identity(url)
    key = hashlib.sha256(f"{repository.lower()}#{number}".encode()).hexdigest()[:32]
    return Path(run_root) / "pr-actions" / key / f"{action_id}.json"


@contextmanager
def submission_lock(path):
    """Serialize submissions, not execution; independent reviewers may still run."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path.parent / "submission.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "PR submission is busy; retry the same action ID"
            ) from None
        yield


def submit_action(
    url,
    resolved,
    *,
    fixtures_only,
    respond,
    github,
    launcher,
    action_id=None,
    expected_head=None,
):
    from afk_pr import jobs

    if action_id is None:
        action_id = uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", action_id):
        raise ValueError(
            "action ID must be 1-64 letters, digits, dots, underscores or hyphens"
        )
    if expected_head is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise ValueError("expected head must be a full lowercase commit SHA")
    if respond and fixtures_only:
        raise ValueError("response cannot be fixtures-only")
    config, slug, project = resolved
    path = receipt_path(config["run_root"], url, action_id)
    kind = "respond" if respond else "fixtures" if fixtures_only else "review"
    with submission_lock(path):
        if path.exists():
            receipt = jobs.read(path)
            if receipt["command"] != kind or (
                expected_head is not None and receipt["head"] != expected_head
            ):
                raise ValueError(
                    "action ID already belongs to a different command or head"
                )
            directory = Path(config["run_root"]) / "pr-reviews" / receipt["job_id"]
            if receipt["state"] != "paused" and (
                receipt["state"] != "submitted" or not (directory / "job.json").exists()
            ):
                receipt.update(
                    state="paused",
                    reason="Submission interrupted or uncertain; inspect retained job before choosing a new action",
                )
                jobs.write(path, receipt)
            result = (
                jobs.status_job(directory) if (directory / "job.json").exists() else {}
            )
            return {**result, "action": receipt}

        context = github.observe(url)
        pr = context["pull_request"]
        if pr["state"] != "open":
            raise ValueError("PR passes require an open PR")
        if expected_head is not None and pr["head"]["sha"] != expected_head:
            raise ValueError("PR head changed; no action submitted")
        if respond:
            from afk_pr.response import response_branch

            response_branch(pr, url)
        job_id = uuid.uuid4().hex[:16]
        repository, number = identity(url)
        receipt = {
            "schema_version": 1,
            "id": action_id,
            "repository": repository.lower(),
            "pr_number": number,
            "command": kind,
            "head": pr["head"]["sha"],
            "base": pr["base"]["sha"],
            "job_id": job_id,
            "state": "preparing",
            "created_at": timestamp(),
        }
        # Reserve the job identity before any job files or external launch effects.
        jobs.write(path, receipt)
        try:
            directory, phases = jobs.prepare_submission(
                url,
                config,
                slug,
                project,
                context,
                job_id,
                action_id,
                fixtures_only=fixtures_only,
                respond=respond,
                github=github,
            )
            current = github.observe(url)["pull_request"]
            if current["state"] != "open" or any(
                current[key]["sha"] != receipt[key] for key in ("head", "base")
            ):
                receipt.update(
                    state="paused",
                    reason="PR changed before launch; no workers submitted",
                )
                jobs.write(path, receipt)
                return {"action": receipt, "directory": str(directory)}
            receipt["state"] = "submitting"
            jobs.write(path, receipt)
            jobs.start(directory, phases, github=github, launcher=launcher)
        except Exception as error:
            receipt.update(
                state="paused",
                reason=f"Submission raised {type(error).__name__}; inspect retained job",
            )
            jobs.write(path, receipt)
            raise RuntimeError(
                f"Action {action_id}, job {job_id}: submission paused ({error}); retry the same action ID to inspect"
            ) from error
        receipt.update(state="submitted", submitted_at=timestamp())
        jobs.write(path, receipt)
        return {**jobs.status_job(directory), "action": receipt}
