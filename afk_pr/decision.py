"""Read-only next-step eligibility for explicitly selected PR jobs."""

import re
import subprocess
from pathlib import Path

from afk_pr.github import GitHub, identity
from afk_pr.review_result import validate


def _selected_id(item):
    if not isinstance(item, dict):
        return None
    job = item.get("job")
    return item.get("requested_id") or (
        job.get("id") if isinstance(job, dict) else None
    )


def decide(context, selected):
    """Join execution facts; never select a repair, merge, or interpret review prose."""
    pr = context["pull_request"]
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    repository, number = identity(pr["html_url"])
    reasons, waiting, retries = [], [], []
    by_id = {
        item["job"]["id"]: item
        for item in selected
        if isinstance(item, dict)
        and isinstance(item.get("job"), dict)
        and isinstance(item["job"].get("id"), str)
    }

    def pause(code, job_id=None, phase=None):
        reasons.append({"code": code, "job_id": job_id, "phase": phase})

    if pr["state"] != "open":
        pause("pr_not_open")
    if not selected:
        pause("no_jobs_selected")
    for item in selected:
        if not isinstance(item, dict):
            pause("evidence_unavailable")
            continue
        job = item.get("job")
        if not isinstance(job, dict) or not job or item.get("error"):
            pause("evidence_unavailable", item.get("requested_id"))
            continue
        job_id = job.get("id", item.get("requested_id"))
        try:
            job_repo, job_number = identity(job["pr_url"])
        except (ValueError, TypeError, KeyError):
            pause("job_identity_unknown", job_id)
            continue
        if (job_repo.lower(), job_number) != (repository.lower(), number):
            pause("job_belongs_to_another_pr", job_id)
            continue
        phases = item.get("phases", {})
        if not isinstance(phases, dict):
            pause("phase_evidence_missing", job_id)
            continue
        kind = job.get("kind")
        expected = job.get("expected_phases")
        allowed = {
            "review": ({"fixtures"}, {"fixtures", "review"}),
            "response": ({"response"},),
            "creation": ({"creation"},),
        }
        if (
            not isinstance(kind, str)
            or not isinstance(expected, list)
            or not expected
            or not all(isinstance(phase, str) for phase in expected)
            or set(expected) not in allowed.get(kind, ())
        ):
            pause("phase_selection_unknown", job_id)
            continue
        action = item.get("action")
        if job.get("action_id"):
            command = (
                "respond"
                if kind == "response"
                else "review"
                if "review" in expected
                else "fixtures"
            )
            if not isinstance(action, dict) or any(
                (
                    action.get("schema_version") != 1,
                    action.get("id") != job["action_id"],
                    action.get("job_id") != job_id,
                    action.get("head") != job.get("head"),
                    action.get("base") != job.get("base"),
                    action.get("repository") != repository.lower(),
                    action.get("pr_number") != number,
                    action.get("command") != command,
                )
            ):
                pause("action_identity_unknown", job_id)
            elif action.get("state") != "submitted":
                pause("action_unsettled", job_id)
        work_phase = "response" if kind == "response" else "creation"
        work = phases.get(work_phase, {}) if kind in {"response", "creation"} else {}
        work = work if isinstance(work, dict) else {}
        progress = work.get("progress", {})
        progress = progress if isinstance(progress, dict) else {}
        revision = (
            progress.get("candidate")
            if progress.get("push") == "pushed"
            else job.get("head")
        )
        # A running push may have reached GitHub before its local confirmation write.
        in_flight_push = (
            work.get("state") == "running"
            and progress.get("push") == "attempted"
            and progress.get("candidate") == head
        )
        if job.get("base") != base or (revision != head and not in_flight_push):
            pause("revision_changed", job_id)
        for phase in expected:
            record = phases.get(phase)
            if not isinstance(record, dict):
                pause("phase_evidence_missing", job_id, phase)
                continue
            state = record.get("state")
            if record.get("worker_observation") == "unavailable":
                pause("worker_ownership_unknown", job_id, phase)
            if state in {"queued", "running"}:
                waiting.append(
                    {"code": "work_pending", "job_id": job_id, "phase": phase}
                )
                continue
            success = "passed" if phase == "fixtures" else "completed"
            if state != success:
                pause(
                    "fixture_failed"
                    if phase == "fixtures"
                    and state in {"failed", "timed_out", "busy", "interrupted"}
                    else "execution_not_successful",
                    job_id,
                    phase,
                )
                continue
            if phase == "fixtures":
                process = record.get("process", {})
                process = process if isinstance(process, dict) else {}
                if (
                    record.get("candidate_unchanged") is not True
                    or type(process.get("exit_code")) is not int
                    or process.get("exit_code") != 0
                    or process.get("timed_out") is not False
                    or process.get("interrupted") is not False
                    or process.get("error") is not None
                    or "error" not in process
                ):
                    pause("fixture_result_inconsistent", job_id, phase)
            elif phase == "review":
                result = record.get("result", {})
                try:
                    if result.get("schema_version") != 1 or result.get(
                        "head"
                    ) != job.get("head"):
                        raise ValueError("review revision unknown")
                    validate(
                        {"summary": result["summary"], "findings": result["findings"]}
                    )
                except (ValueError, KeyError, TypeError, AttributeError):
                    pause("review_result_unknown", job_id, phase)
            else:
                if phase == "response" and progress.get("changed") is False:
                    pause("response_no_change", job_id, phase)
                elif phase == "response" and progress.get("changed") is not True:
                    pause("response_result_unknown", job_id, phase)
                elif progress.get("push") != "pushed":
                    pause("push_unconfirmed", job_id, phase)
                else:
                    child_id = progress.get("fixture_job")
                    child = (
                        by_id.get(child_id, {}).get("job", {})
                        if isinstance(child_id, str)
                        else {}
                    )
                    if (
                        not child
                        or child.get(f"{phase}_job") != job_id
                        or child.get("head") != revision
                        or child.get("kind") != "review"
                        or child.get("expected_phases") != ["fixtures"]
                    ):
                        pause("fixture_child_missing_or_mismatched", job_id, phase)
            publication = record.get("publication")
            if publication in {"pending", "failed"}:
                retries.append(
                    {"code": "publication_incomplete", "job_id": job_id, "phase": phase}
                )
            elif publication != "published":
                pause("publication_state_unknown", job_id, phase)
    recommendation = (
        "pause"
        if reasons
        else "wait"
        if waiting
        else "retry_publication"
        if retries
        else "continue"
    )
    return {
        "schema_version": 1,
        "recommendation": recommendation,
        "pr_url": pr["html_url"],
        "head": head,
        "base": base,
        "observed_at": context.get("observed_at"),
        "selected_job_ids": [_selected_id(item) for item in selected],
        "reasons": reasons
        or waiting
        or retries
        or [{"code": "selected_work_complete", "job_id": None, "phase": None}],
        "publication_retry_job_ids": sorted({item["job_id"] for item in retries})
        if recommendation == "retry_publication"
        else [],
    }


def observe(url, run_root, job_ids, *, github=None):
    """Read selected records and linked fixture children; leave all retained files alone."""
    from afk_pr import jobs

    github = github or GitHub()
    context = github.observe(url)
    context["pull_request"].setdefault("html_url", url)
    root = Path(run_root) / "pr-reviews"
    selected, pending, seen = [], list(job_ids), set()
    while pending:
        job_id = pending.pop(0)
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{16}", job_id):
            raise ValueError("selected job ID must be 16 lowercase hex characters")
        if job_id in seen:
            continue
        seen.add(job_id)
        try:
            item = jobs.status_job(root / job_id)
            if item["job"]["id"] != job_id:
                raise ValueError("job identity mismatch")
            for phase in ("response", "creation"):
                child = (
                    item["phases"].get(phase, {}).get("progress", {}).get("fixture_job")
                )
                if isinstance(child, str) and re.fullmatch(r"[0-9a-f]{16}", child):
                    pending.append(child)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            item = {"error": "evidence_unavailable"}
        selected.append({**item, "requested_id": job_id})
    return {
        "pr_url": url,
        "head": context["pull_request"]["head"]["sha"],
        "jobs": selected,
        "decision": decide(context, selected),
    }
