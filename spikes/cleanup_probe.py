"""Run real cleanup CLI against disposable Git clones and synthetic job records."""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs)


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(tempfile.mkdtemp(prefix="afk-cleanup-xjya-"))
    state, clones, source = (root / name for name in ("state", "clones", "source"))
    source.mkdir()
    run("git", "init", "-b", "main", str(source))
    run("git", "-C", str(source), "config", "user.name", "Disposable test")
    run("git", "-C", str(source), "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("Disposable probe input\n")
    run("git", "-C", str(source), "add", ".")
    run("git", "-C", str(source), "commit", "-m", "probe")
    head = run("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    config = root / "host.toml"
    config.write_text(f'schema_version=1\nstate_root="{state}"\n')
    forbidden = root / "forbidden-calls.log"
    guards = root / "guards"
    guards.mkdir()
    # Any accidental external workflow command fails and leaves evidence.
    for name in ("gh", "bd", "pi", "docker", "docker-compose"):
        path = guards / name
        path.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0" >> "$PROBE_FORBIDDEN_LOG"\nexit 97\n'
        )
        path.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{guards}:{os.environ['PATH']}",
        "PROBE_FORBIDDEN_LOG": str(forbidden),
    }
    cases = []

    def fixture(name, phase="review"):
        job_id = uuid.uuid4().hex[:16]
        directory = state / "pr-reviews" / job_id
        directory.mkdir(parents=True)
        clone = clones / job_id / phase
        clone.parent.mkdir(parents=True)
        run("git", "clone", str(source), str(clone))
        job = {
            "id": job_id,
            "layout": "independent-clones-v1",
            "cleanup_allowed": True,
            "expected_phases": [phase],
            "workspace_root": str(clones),
            "head": head,
            "pr_url": "https://github.com/example/disposable/pull/1",
        }
        write(directory / "job.json", job)
        write(
            directory / f"{phase}.json",
            {"state": "completed", "publication": "published"},
        )
        (directory / f"{phase}.md").write_text("Synthetic published report\n")
        write(
            directory / "context.json",
            {"pull_request": {"state": "open", "merged": False}},
        )
        (directory / "inference").mkdir()
        write(directory / "inference/receipt.json", {"synthetic": True})
        if phase == "response":
            write(
                directory / "response-progress.json",
                {"candidate": head, "changed": False},
            )
        case = {"name": name, "job_id": job_id, "calls": []}
        cases.append(case)
        return case, directory, clone, job

    def call(case, directory, clone, *, dry=True, expected):
        before = {
            str(p.relative_to(directory)): digest(p)
            for p in directory.rglob("*")
            if p.is_file() and p.name not in {"cleanup.json", "lifecycle.lock"}
        }
        argv = [
            sys.executable,
            str(REPO / "afk"),
            "cleanup",
            case["job_id"],
            "--config",
            str(config),
        ]
        if dry:
            argv.append("--dry-run")
        completed = run(*argv, cwd=REPO, env=env, timeout=30)
        result = json.loads(completed.stdout)
        assert result["outcome"] == expected, (case["name"], result)
        assert all(
            (directory / p).is_file() and digest(directory / p) == value
            for p, value in before.items()
        )
        assert clone.exists() == (expected != "removed"), (case["name"], result)
        case["calls"].append(
            {
                "argv": argv,
                "exit_code": completed.returncode,
                "result": result,
                "clone_exists": clone.exists(),
                "evidence_preserved": True,
            }
        )

    case, d, clone, job = fixture("successful_published_open_pr")
    call(case, d, clone, expected="eligible")
    assert not (d / "cleanup.json").exists()
    call(case, d, clone, dry=False, expected="removed")
    call(case, d, clone, dry=False, expected="removed")

    for name in (
        "dirty_response",
        "unpublished_response",
        "failed_response",
        "historical_layout",
        "completed_pr_external_resources",
        "missing_evidence",
        "active_lock",
        "active_unit",
    ):
        phase = "response" if "response" in name else "review"
        case, d, clone, job = fixture(name, phase)
        if name == "dirty_response":
            (clone / "valuable-repair.txt").write_text(
                "Retain this unfinished repair\n"
            )
        elif name == "unpublished_response":
            write(
                d / "response-progress.json",
                {"candidate": head, "changed": True, "push": "uncertain"},
            )
        elif name == "failed_response":
            write(d / "response.json", {"state": "failed", "publication": "published"})
        elif name == "historical_layout":
            # Replace only this freshly-created disposable clone with a real
            # linked worktree, so the historical retention case is concrete.
            shutil.rmtree(clone)
            run(
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "--detach",
                str(clone),
                head,
            )
            assert (clone / ".git").is_file()
            job["layout"] = "historical-worktree"
            write(d / "job.json", job)
        elif name == "completed_pr_external_resources":
            job["cleanup_allowed"] = False
            write(d / "job.json", job)
            write(
                d / "context.json",
                {"pull_request": {"state": "closed", "merged": True}},
            )
        elif name == "missing_evidence":
            (d / "review.md").unlink()
        if name == "active_lock":
            with (d / "lifecycle.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                call(case, d, clone, expected="retained")
                call(case, d, clone, dry=False, expected="retained")
        elif name == "active_unit":
            unit = f"afk-pr-{job['id']}-review"
            run(
                "systemd-run",
                "--user",
                "--collect",
                "--unit",
                unit,
                "/usr/bin/sleep",
                "120",
            )
            try:
                run("systemctl", "--user", "is-active", unit)
                call(case, d, clone, expected="retained")
                call(case, d, clone, dry=False, expected="retained")
            finally:
                run("systemctl", "--user", "stop", unit)
        else:
            call(case, d, clone, expected="retained")
            call(case, d, clone, dry=False, expected="retained")
        if name == "dirty_response":
            assert (
                clone / "valuable-repair.txt"
            ).read_text() == "Retain this unfinished repair\n"

    case, d, clone, job = fixture(
        "published_response_with_passed_fixture_child", "response"
    )
    child_id = uuid.uuid4().hex[:16]
    child = d.parent / child_id
    child.mkdir()
    write(child / "job.json", {"head": head, "response_job": job["id"]})
    write(child / "fixtures.json", {"state": "passed", "publication": "published"})
    write(
        d / "response-progress.json",
        {"candidate": head, "changed": True, "push": "pushed", "fixture_job": child_id},
    )
    call(case, d, clone, expected="eligible")
    call(case, d, clone, dry=False, expected="removed")
    assert (child / "fixtures.json").is_file()
    assert not forbidden.exists(), "unexpected external workflow command"
    result = {
        "root": str(root),
        "source_head": head,
        "cases": cases,
        "external_workflow_calls": [],
        "source_preserved": (source / "tracked.txt").is_file(),
    }
    write(root / "results.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
