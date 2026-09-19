"""Summarize structured GitHub execution facts without interpreting review prose."""

import re

GUIDANCE = (
    "Read execution-summary.json first for observed execution facts, then the full PR story. "
    "Reviews and comments are opinions at their recorded times, not current test status. "
    "Fixtures may finish after this observation; do not turn pending or unknown into failure. "
    "Distinct runs/producers remain distinct. Unknown profiles or input identities do not "
    "authorize test reuse. This summary is reported GitHub evidence, not an approval. "
)


def summarize(context):
    """Keep the latest update per status context/producer, not per PR or job family."""
    pr = context["pull_request"]
    head = pr["head"]["sha"]
    statuses = []
    latest = {}
    for item in context.get("statuses", []):
        creator = item.get("creator") or {}
        producer = creator.get("id") or creator.get("login")
        name = item.get("context")
        time = item.get("updated_at") or item.get("created_at")
        # Incomplete provenance cannot safely supersede another observation.
        if not producer or not name or not time:
            statuses.append(item)
            continue
        key = (name, producer, item.get("sha", head))
        order = (time, item.get("id") or 0)
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, item)
    statuses.extend(item for _, item in latest.values())
    status_records = []
    for item in statuses:
        name = item.get("context")
        match = re.fullmatch(r"afk/fixtures/([0-9a-f]{16})", name or "")
        sha = item.get(
            "sha", head
        )  # Commit statuses were fetched from this head's endpoint.
        status_records.append(
            {
                "id": item.get("id"),
                "context": name,
                "producer": {
                    key: (item.get("creator") or {}).get(key) for key in ("id", "login")
                },
                "head": sha,
                "current_head": sha == head,
                "state": item.get("state"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "url": item.get("target_url") or item.get("url"),
                "fixture_job_id": match[1] if match else None,
                "profile": None,
                "input_identity": None,
            }
        )
    return {
        "schema_version": 1,
        "observed_at": context.get("observed_at"),
        "pr_url": pr.get("html_url"),
        "head": head,
        "base": pr["base"]["sha"],
        "statuses": status_records,
        "checks": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "producer": {
                    key: (item.get("app") or {}).get(key) for key in ("id", "slug")
                },
                "head": item.get("head_sha"),
                "current_head": item.get("head_sha") == head
                if item.get("head_sha")
                else None,
                "state": item.get("status"),
                "conclusion": item.get("conclusion"),
                "started_at": item.get("started_at"),
                "completed_at": item.get("completed_at"),
                "url": item.get("html_url") or item.get("details_url"),
                "profile": None,
                "input_identity": None,
            }
            for item in context.get("checks", [])
        ],
        "reviews": [
            {
                "id": item.get("id"),
                "reviewer": (item.get("user") or {}).get("login"),
                "head": item.get("commit_id"),
                "current_head": item.get("commit_id") == head
                if item.get("commit_id")
                else None,
                "event": item.get("state"),
                "submitted_at": item.get("submitted_at"),
                "url": item.get("html_url"),
            }
            for item in context.get("reviews", [])
        ],
        "limits": [
            "Observed GitHub records only; no artifact inspection or overall readiness verdict.",
            "Distinct jobs/check runs/producers are not interchangeable; profile and input identity are unknown.",
            "Review events do not identify actionable findings; see the full review text.",
            "Empty or pending evidence is not a failure or a pass. Results may arrive after observation.",
        ],
    }


def freeze(directory, context):
    """Write a small independent inference input beside the untouched full context."""
    from afk_pr.jobs import write

    path = (directory / "execution-summary.json").absolute()
    write(path, summarize(context))
    return str(path)
