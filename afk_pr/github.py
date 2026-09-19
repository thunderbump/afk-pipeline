"""Read complete PR context and publish ordinary GitHub feedback via gh."""

import json
import re
import subprocess
from datetime import datetime, timezone

PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?\Z")


def identity(url):
    match = PR_URL.fullmatch(url)
    if not match:
        raise ValueError("expected https://github.com/OWNER/REPO/pull/NUMBER")
    return match[1], int(match[2])


class GitHub:
    def api(self, endpoint, *, data=None, method=None, pages=False):
        method = method or ("POST" if data is not None else "GET")
        command = ["gh", "api", endpoint, "--method", method]
        if data is not None:
            command += ["--input", "-"]
        if pages:
            command += ["--paginate"]
        result = subprocess.run(
            command,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            # gh stderr may contain request content; retain it only in worker logs.
            raise RuntimeError(f"GitHub request failed: {method or 'GET'} {endpoint}")
        if not pages:
            return json.loads(result.stdout)
        # Older gh releases emit adjacent JSON documents for --paginate.
        decoder = json.JSONDecoder()
        remaining = result.stdout.lstrip()
        values = []
        while remaining:
            value, end = decoder.raw_decode(remaining)
            values.append(value)
            remaining = remaining[end:].lstrip()
        return values

    def collection(self, endpoint, key=None):
        pages = self.api(endpoint, pages=True)
        return [item for page in pages for item in (page[key] if key else page)]

    def observe(self, url):
        repo, number = identity(url)
        path = f"repos/{repo}"
        pr = self.api(f"{path}/pulls/{number}")
        sha = pr["head"]["sha"]
        context = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "pull_request": pr,
            "comments": self.collection(
                f"{path}/issues/{number}/comments?per_page=100"
            ),
            "reviews": self.collection(f"{path}/pulls/{number}/reviews?per_page=100"),
            "review_comments": self.collection(
                f"{path}/pulls/{number}/comments?per_page=100"
            ),
            "commits": self.collection(f"{path}/pulls/{number}/commits?per_page=100"),
            "checks": self.collection(
                f"{path}/commits/{sha}/check-runs?per_page=100&filter=all", "check_runs"
            ),
            "statuses": self.collection(f"{path}/commits/{sha}/statuses?per_page=100"),
        }
        if len(context["commits"]) < pr.get("commits", 0):
            raise ValueError("GitHub did not return the complete PR commit list")
        for check in context["checks"]:
            if check.get("output", {}).get("annotations_count", 0):
                check["annotations"] = self.collection(
                    f"{path}/check-runs/{check['id']}/annotations?per_page=100"
                )
        current = self.api(f"{path}/pulls/{number}")
        if current["head"]["sha"] != sha or current["base"]["sha"] != pr["base"]["sha"]:
            raise ValueError("PR changed while reading context; retry the observation")
        from afk_pr.execution import summarize

        return {"execution_summary": summarize(context), **context}

    def head(self, url):
        repo, number = identity(url)
        return self.api(f"repos/{repo}/pulls/{number}")["head"]["sha"]

    def fixture_status(self, job, state, description):
        repo, _ = identity(job["pr_url"])
        # Each execution retains its own status context; an older execution cannot
        # overwrite a newer run's result for the same commit.
        self.api(
            f"repos/{repo}/statuses/{job['head']}",
            data={
                "state": state,
                "context": f"afk/fixtures/{job['id']}",
                "description": description[:140],
                "target_url": job["pr_url"],
            },
        )

    def fixture_summary(self, job, body):
        return self.comment(job, body, "fixtures")

    def comment(self, job, body, kind):
        repo, number = identity(job["pr_url"])
        marker = f"<!-- afk-{kind}:{job['id']} -->"
        login = self.api("user")["login"]
        comments = self.collection(
            f"repos/{repo}/issues/{number}/comments?per_page=100"
        )
        existing = next(
            (
                c
                for c in comments
                if marker in c["body"] and c["user"]["login"] == login
            ),
            None,
        )
        payload = {"body": marker + "\n" + body}
        if existing:
            result = self.api(
                f"repos/{repo}/issues/comments/{existing['id']}",
                data=payload,
                method="PATCH",
            )
        else:
            result = self.api(f"repos/{repo}/issues/{number}/comments", data=payload)
        return result["html_url"]

    def review(self, job, name, body):
        repo, number = identity(job["pr_url"])
        marker = f"<!-- afk-review:{job['id']}:{name} -->"
        login = self.api("user")["login"]
        reviews = self.collection(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        existing = next(
            (r for r in reviews if marker in r["body"] and r["user"]["login"] == login),
            None,
        )
        if existing:
            return existing["html_url"]
        result = self.api(
            f"repos/{repo}/pulls/{number}/reviews",
            data={
                "commit_id": job["head"],
                "event": "COMMENT",
                "body": marker + "\n" + body,
            },
        )
        return result["html_url"]
