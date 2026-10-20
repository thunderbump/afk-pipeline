import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from afk_inference import Capability, FixtureAdapter, InferenceRuntime, ScriptedResult
from afk_pr import jobs, quickchecks
from tests.quickcheck_fixture import policy


class QuickCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        jobs.git(self.repo, "config", "user.name", "Fixture")
        jobs.git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "sample.cpp").write_text("base\n")
        jobs.git(self.repo, "add", ".")
        jobs.git(self.repo, "commit", "-qm", "base")
        head = jobs.git(self.repo, "rev-parse", "HEAD")
        self.job = {
            "id": "1234567890abcdef",
            "base": head,
            "head": head,
            "review_timeout": 180,
            "quickcheck": policy(),
        }
        self.directory = self.root / "job"
        self.directory.mkdir()

    def session(self, mode="normal", endpoint=None):
        self.job["quickcheck"] = policy(mode, endpoint)
        session = quickchecks.Session(self.directory, self.job, self.repo, "creation")
        self.addCleanup(session.close)
        return session

    def test_same_session_receives_source_failure_repairs_and_reruns_within_quota(self):
        session = self.session()
        source = self.repo / "sample.cpp"
        source.write_text("BROKEN\n")
        first = quickchecks.run(session.path)
        self.assertEqual(first["development_status"], "source_failed")
        self.assertIn("sample.cpp:1:", first["checks"][-1]["diagnostic"])
        source.write_text("fixed\n")
        second = quickchecks.run(session.path)
        self.assertEqual(second["development_status"], "completed")
        self.assertNotEqual(
            first["input_snapshot_sha256"], second["input_snapshot_sha256"]
        )
        self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        self.assertEqual(jobs.read(session.path)["calls"], 2)
        session.close()
        self.assertFalse(session.blocker)

    def test_stale_and_infrastructure_feedback_are_not_success(self):
        for mode, expected in (("stale", "stale"), ("infra", "unavailable")):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                job = {**self.job, "quickcheck": policy(mode)}
                session = quickchecks.Session(directory, job, self.repo, "response")
                self.assertEqual(
                    quickchecks.run(session.path)["development_status"], expected
                )
                session.close()
                self.assertFalse(session.blocker)

    def test_no_adapter_and_closed_or_expired_lease_never_launch(self):
        self.assertIsNone(
            quickchecks.Session(self.directory, {}, self.repo, "response").command
        )
        session = self.session()
        with quickchecks.lease(session.path) as state:
            state["deadline"] = time.monotonic() + 10
        with mock.patch("afk_pr.quickchecks.subprocess.Popen") as launch:
            self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
            session.close()
            self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        launch.assert_not_called()

    def test_managed_phase_keeps_inference_budget_and_reserves_cleanup(self):
        jobs.write(self.directory / "job.json", self.job)
        with mock.patch("afk_pr.jobs.subprocess.run") as launch:
            jobs.launch(self.directory, "creation", 180)
        command = launch.call_args.args[0]
        self.assertIn("--property=RuntimeMaxSec=1260", command)
        self.assertIn("--property=TimeoutStopSec=932", command)

    def test_bad_or_unconfirmed_submitted_result_blocks_publication(self):
        for mode in ("malformed", "no-cleanup"):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                session = quickchecks.Session(
                    directory,
                    {**self.job, "quickcheck": policy(mode)},
                    self.repo,
                    "response",
                )
                report = quickchecks.run(session.path)
                self.assertEqual(
                    report.get("development_status", report.get("status")),
                    "unavailable",
                )
                session.close()
                self.assertTrue(session.blocker)

    def test_tool_drift_is_unavailable_before_vm_and_does_not_block_safe_publication(
        self,
    ):
        session = self.session("tool-drift")
        self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        session.close()
        self.assertFalse(session.blocker)
        self.assertFalse((session.path.parent / "1/quick.stdout.json").exists())

    def test_timeout_terminates_same_group_client_and_blocks_unconfirmed_cleanup(self):
        self.job["quickcheck"] = {**policy("sleep"), "timeout_seconds": 1}
        session = quickchecks.Session(self.directory, self.job, self.repo, "response")
        report = quickchecks.run(session.path)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("timed out", report["reason"])
        self.assertTrue(jobs.read(session.path)["active"])
        self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        self.assertEqual(jobs.read(session.path)["calls"], 1)
        session.close()
        self.assertTrue(session.blocker)
        self.assertIsNone(jobs.read(session.path)["child"])
        self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        self.assertEqual(jobs.read(session.path)["calls"], 1)

    def test_failed_termination_retains_owned_child_for_finally_retry(self):
        endpoint = self.root / "retry.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(endpoint))
            server.listen(1)
            server.settimeout(5)
            self.job["quickcheck"] = {
                **policy("socket", endpoint),
                "timeout_seconds": 1,
            }
            session = quickchecks.Session(
                self.directory, self.job, self.repo, "response"
            )
            spawn = subprocess.Popen
            children = []

            def retain_child(*args, **kwargs):
                child = spawn(*args, **kwargs)
                children.append(child)
                return child

            with (
                mock.patch(
                    "afk_pr.quickchecks.stop_owned",
                    side_effect=OSError("fixture signal failure"),
                ),
                mock.patch(
                    "afk_pr.quickchecks.subprocess.Popen", side_effect=retain_child
                ),
            ):
                self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
            child = jobs.read(session.path)["child"]
            self.assertIsNotNone(child)
            self.assertEqual(
                quickchecks.process_identity(child["pid"]), child["started"]
            )
            session.close()
            for process in children:
                process.wait(timeout=2)
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                self.assertEqual(connection.recv(1), b"")
            self.assertTrue(session.blocker)
            self.assertIsNone(quickchecks.process_identity(child["pid"]))

    def test_close_io_error_blocks_without_masking_an_inference_exception(self):
        session = self.session()
        with (
            mock.patch(
                "afk_pr.quickchecks.lease", side_effect=OSError("fixture I/O error")
            ),
            self.assertRaisesRegex(RuntimeError, "original inference failure"),
        ):
            try:
                raise RuntimeError("original inference failure")
            finally:
                session.close()
        self.assertTrue(session.blocker)

    def test_metadata_registration_failure_terminates_and_reaps_started_child(self):
        session = self.session("metadata-sleep")
        original_lease, spawn = quickchecks.lease, subprocess.Popen
        accesses, children = 0, []

        def fail_registration(path):
            nonlocal accesses
            accesses += 1
            if accesses == 2:
                raise OSError("fixture ownership registration failure")
            return original_lease(path)

        def retain_child(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        with (
            mock.patch("afk_pr.quickchecks.lease", side_effect=fail_registration),
            mock.patch("afk_pr.quickchecks.subprocess.Popen", side_effect=retain_child),
        ):
            self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        state = jobs.read(session.path)
        self.assertIsNone(state["child"])
        self.assertTrue(state["cleanup_confirmed"])
        session.close()
        self.assertFalse(session.blocker)

    def test_unreaped_metadata_child_blocks_readmission_and_keeps_exact_ownership(self):
        session = self.session("metadata-sleep")
        original_lease, spawn = quickchecks.lease, subprocess.Popen
        accesses, children = 0, []

        def fail_registration(path):
            nonlocal accesses
            accesses += 1
            if accesses == 2:
                raise OSError("fixture ownership registration failure")
            return original_lease(path)

        def retain_child(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        try:
            with (
                mock.patch("afk_pr.quickchecks.lease", side_effect=fail_registration),
                mock.patch(
                    "afk_pr.quickchecks.subprocess.Popen", side_effect=retain_child
                ),
                mock.patch(
                    "afk_pr.quickchecks.stop_owned",
                    side_effect=OSError("fixture termination failure"),
                ),
            ):
                self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
            original = jobs.read(session.path)["child"]
            self.assertEqual(original["pid"], children[0].pid)
            self.assertEqual(
                original["started"], quickchecks.process_identity(children[0].pid)
            )
            self.assertFalse(jobs.read(session.path)["cleanup_confirmed"])
            self.assertFalse(jobs.read(session.path)["pending"])
            # Local ownership is a separate admission condition even if a
            # metadata-only operation is recorded as having no remote VM.
            with quickchecks.lease(session.path) as state:
                state["cleanup_confirmed"] = True
            self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
            self.assertEqual(jobs.read(session.path)["calls"], 1)
            self.assertEqual(jobs.read(session.path)["child"], original)
            self.assertIsNone(children[0].poll())
            session.close()
            children[0].wait(timeout=2)
            self.assertTrue(session.blocker)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)

    def test_runtime_grants_only_fixed_command_to_writing_implementation_purposes(self):
        session = self.session()
        arguments = {
            "purpose": "attempt",
            "trusted_task_instructions": "Edit source.",
            "untrusted_task_data": {},
            "requested_capability": Capability.WRITE,
            "execution_root": self.repo,
            "timeout_seconds": 1,
            "evidence_directory": self.root / "inference",
            "validator": lambda value: value,
            "adapter": FixtureAdapter((ScriptedResult(response="done"),)),
            "development_check_command": session.command,
        }
        result = InferenceRuntime().invoke(**arguments)
        self.assertEqual(result.outcome, "succeeded")
        prompt = jobs.read(self.root / "inference/prompt.json")
        self.assertIn("exactly this trusted development-check argv", prompt["system"])
        self.assertIn("becomes stale after edits", prompt["system"])
        for change in (
            {"requested_capability": Capability.READ_ONLY},
            {"purpose": "review"},
            {
                "development_check_command": (
                    sys.executable,
                    str(quickchecks.ROOT / "afk"),
                    "other",
                    str(session.path),
                )
            },
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                InferenceRuntime().invoke(**{**arguments, **change})

    def test_finally_cancellation_closes_actual_socket_without_detaching_client(self):
        endpoint = self.root / "worker.sock"
        connected, disconnected = threading.Event(), threading.Event()
        peer = {}
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(endpoint))
            server.listen(1)
            server.settimeout(5)

            def observe():
                connection, _ = server.accept()
                with connection:
                    peer.update(json.loads(connection.recv(4096)))
                    connected.set()
                    disconnected.set() if connection.recv(1) == b"" else None

            observer = threading.Thread(target=observe)
            observer.start()
            session = self.session("socket", endpoint)
            feedback = []
            runner = threading.Thread(
                target=lambda: feedback.append(quickchecks.run(session.path))
            )
            runner.start()
            self.assertTrue(connected.wait(5))
            self.assertEqual(peer["group"], os.getpgrp())
            self.assertEqual(quickchecks.run(session.path)["status"], "unavailable")
            self.assertEqual(jobs.read(session.path)["calls"], 1)
            session.close()
            runner.join(5)
            observer.join(5)
            self.assertFalse(runner.is_alive())
            self.assertTrue(disconnected.is_set())
            self.assertTrue(session.blocker)
            self.assertIsNone(quickchecks.process_identity(peer["pid"]))
            self.assertEqual((self.repo / "sample.cpp").read_text(), "base\n")

    def test_pid_start_mismatch_never_signals_unrelated_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            quickchecks.stop_owned(child.pid, "wrong-start")
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=2)
