"""Offline qsqc experiment. All adapters are in-memory; no external commands."""

import argparse
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Intent:
    pr: str = "https://github.com/example/project/pull/1"
    head: str = "a" * 40
    base: str = "main"
    method: str = "merge"
    close_bead: str | None = None


@dataclass
class FakeGitHub:
    head: str = "a" * 40
    base: str = "main"
    merged: bool = False
    state: str = "open"
    behavior: str = "merge"
    associations: list = field(default_factory=lambda: ["example-parent"])
    calls: list = field(default_factory=list)

    def observe(self, pr):
        self.calls.append(["observe", pr])
        return {
            "pr": pr,
            "head": self.head,
            "base": self.base,
            "merged": self.merged,
            "state": self.state,
            "associations": list(self.associations),
        }

    def merge(self, intent):
        self.calls.append(["merge", asdict(intent)])
        # Simulate server-side expected-head enforcement, including a race.
        if self.behavior == "race":
            self.head = "b" * 40
        if self.head != intent.head:
            raise RuntimeError("head mismatch")
        if self.behavior == "deny":
            raise RuntimeError("repository policy denied merge")
        if self.behavior == "queue":
            return  # Success means queued, not merged.
        self.merged, self.state = True, "closed"
        if self.behavior == "lost_reply":
            raise RuntimeError("merge response lost")


@dataclass
class FakeBeads:
    states: dict = field(
        default_factory=lambda: {"example-parent": "open", "example-followup": "open"}
    )
    fail_close: bool = False
    calls: list = field(default_factory=list)

    def read(self, bead):
        self.calls.append(["read", bead])
        return self.states[bead]

    def close(self, bead, reason):
        self.calls.append(["close", bead, reason])
        if self.fail_close:
            raise RuntimeError("tracker unavailable")
        self.states[bead] = "closed"


def preview(github, *, close_bead=None):
    """Association is a hint. Only an explicit selection becomes a closure target."""
    observed = github.observe(Intent().pr)
    intent = Intent(
        pr=observed["pr"],
        head=observed["head"],
        base=observed["base"],
        close_bead=close_bead,
    )
    return intent, {"intent": asdict(intent), "observed": observed}


def mismatch(intent, observed):
    return any(getattr(intent, key) != observed[key] for key in ("pr", "head", "base"))


def request_merge(intent, github):
    """Reconcile external state on every call; never infer merge from exit status."""
    observed = github.observe(intent.pr)
    if mismatch(intent, observed):
        return {"merge": "changed", "observed": observed}
    if observed["merged"]:
        return {"merge": "confirmed", "observed": observed}
    if observed["state"] != "open":
        return {"merge": "closed_unmerged", "observed": observed}
    error = None
    try:
        github.merge(intent)
    except RuntimeError as failure:
        error = str(failure)
    observed = github.observe(intent.pr)
    state = (
        "changed"
        if mismatch(intent, observed)
        else "confirmed"
        if observed["merged"]
        else "rejected_or_unknown"
        if error
        else "pending"
    )
    return {"merge": state, "observed": observed, "error": error}


def close_after_merge(intent, github, beads):
    """Separate closure can be retried using the same explicit intent."""
    if intent.close_bead is None:
        return {"closure": "not_requested"}
    observed = github.observe(intent.pr)
    if mismatch(intent, observed) or not observed["merged"]:
        return {"closure": "not_confirmed"}
    if beads.read(intent.close_bead) == "closed":
        return {"closure": "already_closed"}
    try:
        beads.close(intent.close_bead, f"Completed by merged PR {intent.pr}")
    except RuntimeError as failure:
        return {"closure": "failed", "error": str(failure)}
    return {"closure": "closed"}


def finish(intent, github, beads):
    result = request_merge(intent, github)
    result["closure"] = "not_attempted"
    if result["merge"] == "confirmed":
        result.update(close_after_merge(intent, github, beads))
    return result


SCENARIOS = (
    "ordinary",
    "changed_head",
    "changed_base",
    "head_race",
    "denied",
    "already_merged",
    "ambiguous_association",
    "explicit_followup",
    "closure_failure_retry",
    "repeated_success",
    "queued_retry",
    "lost_merge_reply",
    "closed_unmerged",
)


def run_case(name, mode):
    github, beads = FakeGitHub(), FakeBeads()
    selected = None if name == "ambiguous_association" else "example-followup"
    if name == "ambiguous_association":
        github.associations.append("example-followup")
    if name == "already_merged":
        github.merged, github.state = True, "closed"
    if name == "closed_unmerged":
        github.state = "closed"
    intent, initial = preview(github, close_bead=selected)
    if name == "changed_head":
        github.head = "b" * 40
    if name == "changed_base":
        github.base = "release"
    github.behavior = {
        "head_race": "race",
        "denied": "deny",
        "queued_retry": "queue",
        "lost_merge_reply": "lost_reply",
    }.get(name, "merge")
    beads.fail_close = name == "closure_failure_retry"

    def call():
        if mode == "combined":
            return finish(intent, github, beads)
        result = request_merge(intent, github)
        # Separate operator invocation independently verifies merge, even if
        # the preceding command failed or merely queued the request.
        result.update(close_after_merge(intent, github, beads))
        return result

    results = [call()]
    if name in {"closure_failure_retry", "repeated_success", "queued_retry"}:
        beads.fail_close = False
        if name == "queued_retry":
            github.merged, github.state = True, "closed"
        results.append(call())
    return {
        "scenario": name,
        "mode": mode,
        "preview": initial,
        "results": results,
        "github_calls": github.calls,
        "beads_calls": beads.calls,
        "beads": beads.states,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("combined", "separate", "both"), default="both"
    )
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args()
    modes = ("combined", "separate") if args.mode == "both" else (args.mode,)
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    print(
        json.dumps(
            [run_case(case, mode) for case in scenarios for mode in modes], indent=2
        )
    )


if __name__ == "__main__":
    main()
