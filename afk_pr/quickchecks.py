"""One fixed, same-process-group development command within an inference lease."""

import contextlib
import fcntl
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from afk_pr import jobs
from afk_pr.config import quickcheck_policy
from afk_pr.formatting import json_file, sha256

MAX_CALLS = 2
MODEL_RESERVE = 60
CLEANUP_GRACE = 30
ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    "rust-format",
    "rust-dependencies",
    "rust-clippy",
    "rust-tests",
    "native-configure",
    "native-objects",
]


def process_identity(pid):
    try:
        # comm can contain spaces and parentheses; fields after its final ')' are stable.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


@contextlib.contextmanager
def lease(path):
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = jobs.read(path)
        yield state
        jobs.write(path, state)


def stop_owned(pid, started, grace=CLEANUP_GRACE):
    """Use a stable Linux process handle; never signal a reused PID."""
    if not started or process_identity(pid) != started:
        return
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if process_identity(pid) != started:
            return
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        with selectors.DefaultSelector() as observer:
            observer.register(fd, selectors.EVENT_READ)
            if not observer.select(grace):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                observer.select(2)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


class Session:
    """Keep quota outside the editable clone and close it before publication."""

    def __init__(self, directory, job, workspace, phase):
        self.command = None
        self.blocker = False
        self.path = directory / f"{phase}-quickchecks" / "lease.json"
        if "quickcheck" not in job:
            return
        self.path.parent.mkdir(mode=0o700)
        policy = quickcheck_policy(job["quickcheck"])
        workspace = Path(workspace).resolve()
        if Path(policy["command"][0]).resolve().is_relative_to(workspace):
            raise ValueError(
                "Quick-check executable cannot come from the candidate workspace"
            )
        jobs.write(
            self.path,
            {
                "version": 1,
                "active": True,
                "parent_pid": os.getpid(),
                "parent_started": process_identity(os.getpid()),
                "workspace": str(workspace),
                "base": job["base"],
                "head": job["head"],
                "policy": policy,
                "deadline": time.monotonic() + job["review_timeout"],
                "calls": 0,
                "pending": False,
                "cleanup_confirmed": True,
                "child": None,
            },
        )
        self.command = (
            sys.executable,
            str(ROOT / "afk"),
            "dev-check",
            str(self.path.absolute()),
        )

    def close(self):
        if self.command is None:
            return
        try:
            with lease(self.path) as state:
                state["active"] = False
                child = state["child"]
                self.blocker = (
                    self.blocker
                    or state["pending"]
                    or bool(child)
                    or not state["cleanup_confirmed"]
                )
            if child:
                stop_owned(child["pid"], child["started"])
        except (OSError, ValueError, KeyError) as error:
            self.blocker = True
            with contextlib.suppress(OSError):
                jobs.write(
                    self.path.parent / "cleanup-error.json",
                    {"cleanup_confirmed": False, "error": str(error)[:1000]},
                )
        # A dead local client only requests remote cleanup; it cannot prove it.


def invoke_with_checks(directory, job, workspace, phase, **arguments):
    from afk_inference.runtime import invoke

    session = Session(directory, job, workspace, phase)
    if session.command:
        arguments["task_contract_version"] += 1
    try:
        result = invoke(**arguments, development_check_command=session.command)
    finally:
        session.close()
    return result, session.blocker


def unavailable(reason):
    return {
        "version": 1,
        "scope": "afk-development-check",
        "advisory": True,
        "status": "unavailable",
        "reason": reason,
    }


def run(path):
    """Accept only the retained lease; model text supplies no execution arguments."""
    path = Path(path).absolute()
    if (
        path.is_symlink()
        or path.resolve() != path
        or not stat.S_ISREG(path.stat().st_mode)
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("Development lease must be a private regular file")
    with lease(path) as state:
        if (
            not state["active"]
            or process_identity(state["parent_pid"]) != state["parent_started"]
        ):
            return unavailable("The owning inference is no longer active")
        if (
            state["pending"]
            or state["child"] is not None
            or not state["cleanup_confirmed"]
            or state["calls"] >= MAX_CALLS
        ):
            return unavailable(
                "A check is active, cleanup is unconfirmed, or the two-call limit is exhausted"
            )
        remaining = state["deadline"] - time.monotonic() - MODEL_RESERVE - CLEANUP_GRACE
        if remaining <= 0:
            return unavailable(
                "No development time remains inside the inference deadline"
            )
        state.update(calls=state["calls"] + 1, pending=True, cleanup_confirmed=False)
        number = state["calls"]
    directory = path.parent / str(number)
    directory.mkdir(mode=0o700)
    policy, workspace = state["policy"], Path(state["workspace"])
    cleanup = True  # Metadata operations cannot start a VM.
    result = None
    try:

        def execute(name, args, allowed=(0,), limit=30):
            nonlocal cleanup
            timeout = min(
                limit,
                state["deadline"] - time.monotonic() - MODEL_RESERVE - CLEANUP_GRACE,
            )
            if timeout <= 0:
                raise TimeoutError("Development deadline reached")
            stdout, stderr = (
                directory / f"{name}.stdout.json",
                directory / f"{name}.stderr.log",
            )
            with stdout.open("wb") as out, stderr.open("wb") as err:
                if name == "quick":
                    cleanup = False
                started = None
                process = subprocess.Popen(
                    [*policy["command"], *args],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=False,
                )
                try:
                    started = process_identity(process.pid)
                    with lease(path) as current:
                        current["child"] = {"pid": process.pid, "started": started}
                        admitted = current["active"]
                    if not admitted:
                        raise TimeoutError(
                            "Owning inference closed during command launch"
                        )
                    code = process.wait(timeout=timeout)
                except BaseException as error:
                    if started is None:
                        # This function still owns the unreaped Popen child even
                        # when reading or registering its start identity failed.
                        process.terminate()
                        try:
                            process.wait(timeout=CLEANUP_GRACE)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                    else:
                        stop_owned(process.pid, started)
                        process.wait(timeout=2)
                    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                        raise TimeoutError(
                            "Development command timed out; remote cleanup remains unconfirmed"
                        ) from None
                    raise
                finally:
                    running = process.poll() is None
                    if running:
                        cleanup = False
                    with lease(path) as current:
                        current["child"] = (
                            {
                                "pid": process.pid,
                                "started": started or process_identity(process.pid),
                            }
                            if running
                            else None
                        )
            for log in (stdout, stderr):
                log.chmod(0o600)
                if log.stat().st_size > 4 * 1024**2:
                    with log.open("r+b") as stream:
                        stream.truncate(4 * 1024**2)
                    raise ValueError(
                        "Development command output exceeded its private limit"
                    )
            if code not in allowed:
                raise ValueError(
                    "Development command failed; inspect its private diagnostics"
                )
            return stdout, code

        tool = json_file(execute("identity", ["--identity"])[0])
        if (
            type(tool.get("version")) is not int
            or tool.get("version") != 1
            or tool.get("scope") != "working-snapshot-quick-tool"
            or tool.get("checks") != CHECKS
            or not all(
                sha256(tool.get(key))
                for key in ("rust_input_sha256", "native_input_sha256")
            )
            or any(
                tool.get(k) != policy[k]
                for k in ("recipe_sha256", "implementation_sha256")
            )
        ):
            raise ValueError("Development tool identity changed")
        arguments = ["--repo", str(workspace), "--base", state["base"]]
        before = json_file(
            execute("input", [*arguments, "--snapshot-identity"])[0], limit=4 * 1024**2
        )
        if (
            type(before.get("version")) is not int
            or before.get("version") != 1
            or before.get("scope") != "working-snapshot-quick-input"
            or before.get("base") != state["base"]
            or before.get("head") != state["head"]
            or before.get("recipe_sha256") != policy["recipe_sha256"]
            or not sha256(before.get("input_snapshot_sha256"))
        ):
            raise ValueError("Development snapshot does not match its bound worktree")
        output = directory / "result.json"
        _, code = execute(
            "quick",
            [
                *arguments,
                "--expected-input",
                before["input_snapshot_sha256"],
                "--output",
                str(output),
            ],
            allowed=(0, 1, 2),
            limit=policy["timeout_seconds"],
        )
        result = json_file(output, limit=65536)
        expected = {
            "version": 1,
            "scope": "working-snapshot-quickcheck",
            "operation": "quick",
            "advisory": True,
            "exit_code": code,
            "base": state["base"],
            "head": state["head"],
            "input_snapshot_sha256": before["input_snapshot_sha256"],
            "output_snapshot_sha256": before["input_snapshot_sha256"],
            "recipe_sha256": policy["recipe_sha256"],
            "implementation_sha256": policy["implementation_sha256"],
        }
        if any(result.get(k) != value for k, value in expected.items()):
            raise ValueError(
                "Development result identity or advisory scope is malformed"
            )
        partitions = [
            result.get(key)
            for key in (
                "completed_checks",
                "failed_checks",
                "unavailable_checks",
                "skipped_checks",
            )
        ]
        if (
            type(result.get("version")) is not int
            or type(result.get("exit_code")) is not int
            or result.get("advisory") is not True
            or result.get("selected_checks") != CHECKS
            or not all(
                isinstance(part, list) and all(isinstance(name, str) for name in part)
                for part in partitions
            )
            or sorted(name for part in partitions for name in part) != sorted(CHECKS)
            or not isinstance(result.get("checks"), list)
            or len(result["checks"]) != len(CHECKS)
            or (code == 0 and (result["failed_checks"] or result["unavailable_checks"]))
            or (code in (0, 1) and result.get("vm_started") is not True)
            or any(
                result.get(key) != tool.get(key)
                for key in ("rust_input_sha256", "native_input_sha256")
            )
        ):
            raise ValueError(
                "Development result lacks truthful complete stage coverage"
            )
        expected_statuses = {
            name: status
            for status, names in zip(
                ("completed", "failed", "unavailable", "skipped"), partitions
            )
            for name in names
        }
        if {
            item["name"]: item["status"] for item in result["checks"]
        } != expected_statuses:
            raise ValueError(
                "Development stage diagnostics contradict their completion summary"
            )
        cleanup = result.get("cleanup_complete") is True
        current = json_file(
            execute("current", [*arguments, "--snapshot-identity"])[0],
            limit=4 * 1024**2,
        )
        status = (
            "unavailable"
            if not cleanup
            else "stale"
            if current.get("input_snapshot_sha256") != before["input_snapshot_sha256"]
            else "completed"
            if code == 0
            else "source_failed"
            if code == 1
            else "unavailable"
        )
        result = {**result, "development_status": status}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        result = unavailable(str(error)[:1000])
    finally:
        with lease(path) as current:
            current.update(
                pending=False, cleanup_confirmed=cleanup and current["child"] is None
            )
        jobs.write(
            directory / "feedback.json",
            result or unavailable("Development call interrupted"),
        )
    return result
