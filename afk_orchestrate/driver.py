"""A durable, supervised driver using only the independent commands' JSON interfaces."""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"[0-9a-f]{40}")
JOB = re.compile(r"[0-9a-f]{16}")


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    """Publish one durable snapshot before allowing the next external effect."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path, *, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


class CommandFailure(RuntimeError):
    def __init__(self, command, diagnostic=None):
        super().__init__(f"{command} failed; inspect retained command diagnostics")
        self.command = command
        self.diagnostic = diagnostic


class Commands:
    def __init__(self, config, evidence_directory=None):
        self.config = str(config)
        self.evidence_directory = evidence_directory

    def failed(self, command, stdout="", stderr="", returncode=None, error=None):
        diagnostic = None
        if self.evidence_directory is not None:
            directory = Path(self.evidence_directory) / "command-errors"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, filename = tempfile.mkstemp(
                prefix=command + "-", suffix=".json", dir=directory
            )
            os.close(fd)
            diagnostic = str(Path(filename).absolute())

            def bounded(value):
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                return (value or "")[-32768:]

            write(
                Path(filename),
                {
                    "at": now(),
                    "command": command,
                    "returncode": returncode,
                    "error": error,
                    "stdout_tail": bounded(stdout),
                    "stderr_tail": bounded(stderr),
                },
            )
        raise CommandFailure(command, diagnostic)

    def __call__(self, *arguments):
        command = [sys.executable, str(ROOT / "afk"), *arguments]
        if arguments[0] != "context":
            command += ["--config", self.config]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.failed(
                arguments[0],
                getattr(error, "stdout", ""),
                getattr(error, "stderr", ""),
                error=type(error).__name__,
            )
        try:
            value = json.loads(result.stdout)
        except ValueError:
            self.failed(
                arguments[0],
                result.stdout,
                result.stderr,
                result.returncode,
                "invalid_json",
            )
        if (
            result.returncode
            or not isinstance(value, dict)
            or value.get("outcome") == "failed"
        ):
            self.failed(
                arguments[0],
                result.stdout,
                result.stderr,
                result.returncode,
                "command_failed",
            )
        return value


def record(state, event, **details):
    state["updated_at"] = now()
    state["events"].append({"at": state["updated_at"], "event": event, **details})


def transition(state, stage):
    state["stage"] = stage
    record(state, "transition", stage=stage)


def pause(state, reason, details=None):
    state.update(status="paused", reason=reason, details=details)
    record(state, "paused", reason=reason)


def revision(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("missing or invalid candidate revision")
    return value


def job_id(result):
    value = result["job"]["id"]
    if not isinstance(value, str) or not JOB.fullmatch(value):
        raise ValueError("missing or invalid job receipt")
    return value


def action_id(state, command):
    return f"orch-{state['id']}-{state['generation']}-{command}-{state['repairs']}"


def repairable_validation(result):
    """Accept completed, published nonzero fixture exits, never uncertain execution.

    Status remains a read-only eligibility report. Only this supervisor chooses
    to spend a repair on failed validation; respond reads the published evidence.
    """
    decision = result["decision"]
    reasons = decision.get("reasons", [])
    if (
        decision.get("recommendation") != "pause"
        or not reasons
        or any(reason.get("code") != "fixture_failed" for reason in reasons)
    ):
        return False
    failed = set()
    for item in result["jobs"]:
        for phase, record in item["phases"].items():
            if record.get("publication") != "published":
                return False
            if phase != "fixtures":
                if record.get("state") != "completed":
                    return False
                continue
            process = record.get("process", {})
            code = process.get("exit_code")
            if (
                record.get("candidate_unchanged") is not True
                or type(code) is not int
                or process.get("timed_out") is not False
                or process.get("interrupted") is not False
                or "error" not in process
                or process["error"] is not None
            ):
                return False
            if record.get("state") == "failed" and code != 0:
                failed.add(item["job"]["id"])
            elif record.get("state") != "passed" or code != 0:
                return False
    return bool(failed) and failed == {
        reason.get("job_id") for reason in reasons if reason.get("phase") == "fixtures"
    }


def request_repair(state):
    if state["repairs"] >= state["max_repairs"]:
        pause(state, "repair_limit_reached")
    else:
        state["repairs"] += 1
        transition(state, "response_submit")


def selected(state, commands, job):
    result = commands("status", state["pr_url"], "--job", job)
    decision = result["decision"]
    if decision.get("recommendation") == "wait":
        return None
    # A failed fixture can finish before its concurrent reviewer. Let that
    # already-submitted work settle before selecting another response.
    if (
        decision.get("recommendation") == "pause"
        and decision.get("reasons")
        and all(
            reason.get("code") == "fixture_failed" for reason in decision["reasons"]
        )
        and any(
            (
                phase.get("state") in {"queued", "running"}
                or (
                    phase.get("publication") == "pending"
                    and phase.get("worker_observation") == "active"
                )
            )
            for item in result["jobs"]
            for phase in item["phases"].values()
        )
    ):
        return None
    if decision.get("recommendation") != "continue" and not repairable_validation(
        result
    ):
        pause(state, "independent_command_requires_attention", decision)
        return None
    if revision(decision.get("base")) != state["base"]:
        pause(state, "base_changed")
        return None
    matches = [item for item in result["jobs"] if item.get("job", {}).get("id") == job]
    if len(matches) != 1:
        raise ValueError("selected job unavailable")
    item = matches[0]
    if item["job"]["head"] != state["head"]:
        raise ValueError("selected job has an unexpected starting head")
    return result, item


def step(state, commands):
    """Perform one transition. Caller holds the run lock and saves after return.

    Submission stages, generation and repair count are saved by the preceding
    transition. A lost command return therefore retries the same action identity.
    """
    if state["status"] != "running":
        return
    stage = state["stage"]
    invoke = commands

    def commands(*arguments):
        value = invoke(*arguments)
        if state.get("observation_failure_command") == arguments[0]:
            state.pop("observation_failures", None)
            state.pop("observation_failure_command", None)
        return value

    try:
        if stage == "creation_submit":
            result = commands("pr", state["bead_id"])
            if "job" not in result:
                if isinstance(result.get("pr_url"), str):
                    state["pr_url"] = result["pr_url"]
                pause(state, "existing_pr_without_creation_receipt", result)
                return
            if result["job"].get("bead_id") != state["bead_id"]:
                raise ValueError("creation belongs to another bead")
            state.update(
                creation_job=job_id(result),
                head=revision(result["job"]["head"]),
                base=revision(result["job"]["base"]),
            )
            transition(state, "creation_wait")
        elif stage == "creation_wait":
            result = commands("job", state["creation_job"])
            if (
                job_id(result) != state["creation_job"]
                or result["job"].get("bead_id") != state["bead_id"]
            ):
                raise ValueError("creation identity changed")
            phase = result["phases"]["creation"]
            if isinstance(phase.get("progress", {}).get("pr_url"), str):
                state["pr_url"] = phase["progress"]["pr_url"]
            if phase.get("worker_observation") == "unavailable":
                pause(state, "creation_worker_unknown")
                return
            if phase["state"] in {"queued", "running"}:
                return
            if (
                phase.get("publication") == "pending"
                and phase.get("worker_observation") == "active"
            ):
                return
            if phase["state"] != "completed" or phase.get("publication") != "published":
                pause(state, "creation_requires_attention", phase)
                return
            state["pr_url"] = phase["progress"]["pr_url"]
            observation = selected(state, commands, state["creation_job"])
            if observation:
                result, item = observation
                candidate = revision(
                    item["phases"]["creation"]["progress"]["candidate"]
                )
                if candidate != result["head"]:
                    raise ValueError("creation candidate no longer current")
                state["head"] = candidate
                if repairable_validation(result):
                    request_repair(state)
                else:
                    transition(state, "review_submit")
        elif stage in {"review_submit", "response_submit"}:
            command = "review" if stage == "review_submit" else "respond"
            result = commands(
                command,
                state["pr_url"],
                "--action-id",
                action_id(state, command),
                "--expected-head",
                state["head"],
            )
            action = result.get("action", {})
            if action.get("state") != "submitted":
                pause(state, "submission_uncertain", action)
                return
            identifier = job_id(result)
            if (
                action.get("command") != command
                or action.get("id") != action_id(state, command)
                or action.get("job_id") != identifier
                or action.get("head") != state["head"]
                or action.get("base") != state["base"]
            ):
                raise ValueError("submission receipt identity mismatch")
            state["active_job"] = identifier
            transition(state, "review_wait" if command == "review" else "response_wait")
        elif stage in {"review_wait", "response_wait"}:
            observation = selected(state, commands, state["active_job"])
            if not observation:
                return
            result, item = observation
            if stage == "response_wait":
                candidate = revision(
                    item["phases"]["response"]["progress"]["candidate"]
                )
                if candidate != result["head"]:
                    raise ValueError("response candidate no longer current")
                state["head"] = candidate
                if repairable_validation(result):
                    request_repair(state)
                else:
                    transition(state, "review_submit")
            else:
                if result["head"] != state["head"]:
                    raise ValueError("review head changed")
                if repairable_validation(result):
                    request_repair(state)
                    return
                review = item["phases"]["review"]["result"]
                if review["head"] != state["head"] or not isinstance(
                    review["findings"], list
                ):
                    raise ValueError("review result unavailable")
                if not review["findings"]:
                    state["status"] = "ready_for_merge"
                    record(
                        state,
                        "ready_for_merge",
                        head=state["head"],
                        review_job=state["active_job"],
                    )
                else:
                    request_repair(state)
        else:
            raise ValueError("unknown orchestration stage")
    except CommandFailure as error:
        details = {
            "command": error.command,
            "diagnostic": error.diagnostic,
            "stage": stage,
        }
        if error.command in {"status", "job", "context"}:
            failures = (
                state.get("observation_failures", 0)
                if state.get("observation_failure_command") == error.command
                else 0
            ) + 1
            state["observation_failure_command"] = error.command
            state["observation_failures"] = failures
            if failures < 3:
                record(state, "observation_retry", attempt=failures, **details)
                return
        pause(state, "command_or_evidence_error", details)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        pause(
            state, "command_or_evidence_error", {"message": str(error), "stage": stage}
        )


def create(root, bead, config, max_repairs=5):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", bead):
        raise ValueError("invalid bead ID")
    if not 0 <= max_repairs <= 5:
        raise ValueError("max repairs must be between 0 and 5")
    identifier = hashlib.sha256(bead.encode()).hexdigest()[:16]
    path = root / identifier / "state.json"

    def existing():
        state = read(path)
        if state["bead_id"] != bead or state["config"] != str(config):
            raise ValueError("existing run configuration differs")
        return path, False

    # Atomic snapshots let repeated start observe an active run without taking
    # the worker's lifetime lock. Recheck after locking for competing creators.
    if path.exists():
        return existing()
    with lock(path):
        if path.exists():
            return existing()
        state = {
            "schema_version": 1,
            "id": identifier,
            "bead_id": bead,
            "config": str(config),
            "status": "running",
            "stage": "creation_submit",
            "repairs": 0,
            "max_repairs": max_repairs,
            "generation": 0,
            "created_at": now(),
            "events": [],
        }
        record(state, "created")
        write(path, state)
    return path, True


def advance(path, commands=None):
    """One locked tick, useful for schedulers and bounded operational checks."""
    with lock(path):
        state = read(path)
        step(state, commands or Commands(state["config"], path.parent))
        write(path, state)
        return state


def resume(path, *, review_current_head=False, commands=None):
    with lock(path):
        state = read(path)
        commands = commands or Commands(state["config"], path.parent)
        if review_current_head:
            context = commands("context", state["pr_url"])["pull_request"]
            if context["state"] != "open":
                raise ValueError("PR must be open")
            state.update(
                head=revision(context["head"]["sha"]),
                base=revision(context["base"]["sha"]),
                generation=state["generation"] + 1,
            )
            transition(state, "review_submit")
            record(state, "operator_selected_current_head", head=state["head"])
        elif state["status"] == "ready_for_merge":
            return state
        state.pop("observation_failures", None)
        state.pop("observation_failure_command", None)
        state.update(status="running", reason=None, details=None)
        record(state, "resumed")
        write(path, state)
        return state
