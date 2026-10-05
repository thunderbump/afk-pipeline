"""Select retained, attributed validation for one PR revision and contract."""

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from afk_pr.github import identity


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def probe_identity(command):
    """Read at most 4096 bytes within ten seconds, including descendant processes."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                data = os.read(process.stdout.fileno(), 4097 - len(output))
                if not data:
                    break
                output.extend(data)
                if len(output) > 4096:
                    return None
        if process.wait(timeout=max(0.001, deadline - time.monotonic())):
            return None
        return json.loads(output)
    except (ValueError, subprocess.SubprocessError):
        return None
    finally:
        # Kill descendants even when the direct process exited or filled stdout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()


def contract(job):
    """Unknown external validator identity disables reuse, without blocking execution.

    Repository entrypoints are bound to the exact candidate and trusted base.
    External adapters must supply a bounded, read-only identity command covering
    their effective release, profile and inputs. Only its digest is retained.
    """
    policy = job.get("validation", {})
    if not isinstance(policy, dict):
        return None
    command = policy.get("command", [])
    if (
        not isinstance(command, list)
        or not command
        or not isinstance(command[0], str)
        or not job.get("pr_url")
    ):
        return None
    external = None
    probe = policy.get("identity_command")
    if probe:
        try:
            value = probe_identity(probe)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", "identity"}
                or type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1
                or not isinstance(value.get("identity"), str)
                or not value["identity"].strip()
                or len(value["identity"].encode()) > 256
            ):
                return None
            external = digest(value["identity"])
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    elif (
        not command[0].startswith("./")
        or ".." in Path(command[0]).parts
        or job.get("fixture_resource")
    ):
        return None
    try:
        repo, number = identity(job["pr_url"])
        if not all(
            isinstance(job.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", job[key])
            for key in ("head", "base")
        ):
            return None
        return {
            "schema_version": 1,
            "repository": repo.lower(),
            "pr_number": number,
            "head": job["head"],
            "base": job["base"],
            "validation": policy,
            "policy": job.get("policy"),
            "resource": job.get("fixture_resource"),
            "execution": execution(job),
            "external_identity": external,
        }
    except (KeyError, TypeError, ValueError):
        return None


def execution(job):
    return {
        key: job.get(key) for key in ("layout", "repository", "remote", "fixture_slot")
    }


@contextmanager
def reservation(directory, job):
    """Serialize validation handoffs for this PR, including creation and response."""
    repo, number = identity(job["pr_url"])
    key = digest([repo.lower(), number])[:32]
    with (directory.parent / f"validation-{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def seal(directory, job, record):
    """Detect later edits to the selected job, result or retained execution logs."""
    files = {}
    for name in ("fixtures.stdout.log", "fixtures.stderr.log"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest({"job": job, "result": record, "logs": files})


def eligible(directory, job, expected, *, probe=True):
    """Share active work or a sealed published pass/failure; never manufacture green."""
    from afk_pr import jobs

    if (
        not isinstance(job, dict)
        or not expected
        or job.get("fixture_contract") != expected
    ):
        return False
    try:
        repo, number = identity(job["pr_url"])
        if any(
            (
                repo.lower() != expected["repository"],
                number != expected["pr_number"],
                job.get("head") != expected["head"],
                job.get("base") != expected["base"],
                job.get("validation") != expected["validation"],
                job.get("policy") != expected["policy"],
                job.get("fixture_resource") != expected["resource"],
                execution(job) != expected["execution"],
            )
        ):
            return False
        if directory.is_symlink() or any(
            (directory / name).is_symlink()
            for name in ("job.json", "fixtures.json", "fixtures-seal.json")
        ):
            return False
        if job.get("id") != directory.name or "fixtures" not in job.get(
            "expected_phases", []
        ):
            return False
        record = jobs.status_job(directory, probe=probe)["phases"]["fixtures"]
        if record.get("worker_observation") == "unavailable":
            return False
        if record.get("state") in {"queued", "running"}:
            return record.get("worker_observation") == "active"
        if (
            record.get("state") not in {"passed", "failed"}
            or record.get("candidate_unchanged") is not True
            or record.get("contract_unchanged") is not True
        ):
            return False
        if record.get("publication") == "pending":
            return record.get("worker_observation") == "active"
        if record.get("publication") != "published" or not record.get("url"):
            return False
        process = record.get("process", {})
        if (
            type(process.get("exit_code")) is not int
            or process.get("timed_out") is not False
            or process.get("interrupted") is not False
            or "error" not in process
            or process["error"] is not None
            or (record["state"] == "passed") != (process["exit_code"] == 0)
        ):
            return False
        saved = jobs.read(directory / "fixtures-seal.json")
        return saved.get("sha256") == seal(directory, job, record) is not None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        return False


def select(directory, job):
    """Choose only matching AFK-owned evidence in this PR's retained job lineage."""
    from afk_pr import jobs

    expected = contract(job)
    if expected is None:
        return None, None
    candidates = []
    for target in directory.parent.iterdir():
        if target == directory or not re.fullmatch(r"[0-9a-f]{16}", target.name):
            continue
        try:
            saved = jobs.read(target / "job.json")
            if eligible(target, saved, expected):
                candidates.append(saved)
        except (OSError, ValueError, TypeError):
            continue
    chosen = max(candidates, key=lambda item: item.get("created_at", ""), default=None)
    return (chosen["id"] if chosen else None), expected
