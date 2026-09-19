import contextlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from afk_pr import actions, jobs
from afk_pr.__main__ import main
from tests.test_pr_passes import SHA, URL, FakeGitHub


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gh = FakeGitHub()
        self.launches = []
        self.config = {"run_root": self.root}
        self.project = {"repository": str(self.root / "repo")}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            mock.patch.object(
                jobs, "settings", return_value=(self.config, "test", self.project)
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                jobs,
                "job_settings",
                return_value={
                    "review_timeout": 30,
                    "validation": {"timeout_seconds": 30},
                },
            )
        )
        self.real_status = jobs.status_job
        self.stack.enter_context(
            mock.patch.object(
                jobs,
                "status_job",
                side_effect=lambda directory: self.real_status(directory, probe=False),
            )
        )

    def submit(self, **kwargs):
        return jobs.submit(
            kwargs.pop("url", URL),
            self.root / "config",
            github=self.gh,
            launcher=kwargs.pop("launcher", lambda *args: self.launches.append(args)),
            **kwargs,
        )

    def test_receipt_precedes_launch_and_lost_return_reuses_job(self):
        def launch(directory, phase, timeout):
            receipt = jobs.read(actions.receipt_path(self.root, URL, "review-1"))
            self.assertEqual(receipt["state"], "submitting")
            self.assertEqual(receipt["job_id"], directory.name)
            self.launches.append((directory, phase))

        with (
            mock.patch.object(
                jobs, "status_job", side_effect=ConnectionError("lost caller reply")
            ),
            self.assertRaises(ConnectionError),
        ):
            self.submit(action_id="review-1", expected_head=SHA, launcher=launch)
        replay = self.submit(action_id="review-1", expected_head=SHA)
        self.assertEqual(replay["action"]["state"], "submitted")
        self.assertEqual(replay["job"]["id"], self.launches[0][0].name)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(
            self.real_status(Path(replay["directory"]), probe=False)["action"],
            replay["action"],
        )

    def test_new_actions_and_unkeyed_calls_remain_distinct(self):
        results = [self.submit(action_id=key) for key in ("one", "two", None, None)]
        self.assertEqual(len({r["job"]["id"] for r in results}), 4)
        self.assertEqual(len(self.launches), 8)

    def test_intent_conflicts_and_invalid_input_do_not_launch(self):
        self.submit(action_id="same")
        for changes in (
            {"fixtures_only": True},
            {"respond": True},
            {"expected_head": "c" * 40},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.submit(action_id="same", **changes)
        for key in ("", "../escape", "x" * 65):
            with self.assertRaises(ValueError):
                self.submit(action_id=key)
        self.assertEqual(len(self.launches), 2)

    def test_revision_change_before_reservation_and_before_launch(self):
        with self.assertRaisesRegex(ValueError, "head changed"):
            self.submit(action_id="wrong", expected_head="c" * 40)
        self.assertFalse(actions.receipt_path(self.root, URL, "wrong").exists())
        for field in ("head", "base"):
            first = self.gh.observe(URL)
            second = self.gh.observe(URL)
            second["pull_request"][field]["sha"] = "c" * 40
            with mock.patch.object(self.gh, "observe", side_effect=[first, second]):
                result = self.submit(action_id=field)
            self.assertEqual(result["action"]["state"], "paused")
            self.assertEqual(self.submit(action_id=field)["action"], result["action"])
            self.assertEqual(
                self.submit(action_id=field)["action"]["job_id"],
                result["action"]["job_id"],
            )
        self.assertFalse(self.launches)

    def test_crash_before_job_files_pauses_without_reallocation(self):
        with (
            mock.patch.object(
                jobs, "prepare_submission", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.submit(action_id="crash")
        before = jobs.read(actions.receipt_path(self.root, URL, "crash"))
        replay = self.submit(action_id="crash")
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(replay["action"]["job_id"], before["job_id"])
        self.assertNotIn("job", replay)
        self.assertFalse(self.launches)

    def test_crash_after_partial_launch_does_not_repeat_any_phase(self):
        def launch(*args):
            self.launches.append(args)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.submit(action_id="partial", launcher=launch)
        replay = self.submit(action_id="partial")
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(len(self.launches), 1)
        self.assertIn("fixtures", replay["phases"])

    def test_crash_before_receipt_completion_keeps_existing_workers(self):
        original = jobs.write

        def write(path, value):
            if path.name == "seal.json" and value.get("state") == "submitted":
                raise KeyboardInterrupt
            original(path, value)

        with (
            mock.patch.object(jobs, "write", side_effect=write),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.submit(action_id="seal")
        replay = self.submit(action_id="seal")
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(len(self.launches), 2)

    def test_accepted_worker_then_launcher_timeout_pauses_without_overwriting_worker(
        self,
    ):
        def launch(directory, phase, timeout):
            self.launches.append((directory, phase))
            jobs.write(
                directory / f"{phase}.json",
                {"state": "running", "publication": "pending"},
            )
            raise subprocess.TimeoutExpired("systemd-run", timeout)

        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.submit(action_id="timeout", launcher=launch)
        path = actions.receipt_path(self.root, URL, "timeout")
        original = jobs.read(path)
        replay = self.submit(action_id="timeout")
        self.assertEqual(replay["action"], original)
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(replay["phases"]["fixtures"]["state"], "running")
        self.assertEqual(len(self.launches), 1)

    def test_sigkill_releases_process_lock_and_keeps_reserved_job_identity(self):
        script = """
import os, signal, sys
from pathlib import Path
from unittest import mock
from afk_pr import jobs
from tests.test_pr_passes import FakeGitHub, URL
root = Path(sys.argv[1])
def crash(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
with mock.patch.object(jobs, 'settings', return_value=({'run_root': root}, 'test', {})), mock.patch.object(jobs, 'prepare_submission', side_effect=crash):
    jobs.submit(URL, root / 'config', action_id='killed', github=FakeGitHub())
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
        original = jobs.read(actions.receipt_path(self.root, URL, "killed"))
        replay = self.submit(action_id="killed")
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(replay["action"]["job_id"], original["job_id"])
        self.assertFalse(self.launches)

    def test_concurrent_submission_is_busy_then_reuses_existing_job(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def launch(*args):
            self.launches.append(args)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timed out")

        def first():
            try:
                results.append(self.submit(action_id="concurrent", launcher=launch))
            except (ValueError, RuntimeError, AssertionError) as error:
                errors.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, "busy"):
                self.submit(action_id="concurrent")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(
            self.submit(action_id="concurrent")["job"]["id"], results[0]["job"]["id"]
        )
        self.assertEqual(len(self.launches), 2)

    def test_url_case_reuses_receipt_and_submitted_replay_does_not_reobserve_head(self):
        first = self.submit(action_id="case")
        with mock.patch.object(
            self.gh, "observe", side_effect=AssertionError("replay must not reobserve")
        ):
            replay = self.submit(
                action_id="case",
                url=URL.replace("example/repository", "Example/Repository"),
            )
        self.assertEqual(first["job"]["id"], replay["job"]["id"])

    def test_missing_submitted_job_is_uncertain_not_a_success(self):
        first = self.submit(action_id="missing")
        (Path(first["directory"]) / "job.json").unlink()
        replay = self.submit(action_id="missing")
        self.assertEqual(replay["action"]["state"], "paused")
        self.assertEqual(len(self.launches), 2)

    def test_cli_options_and_paused_exit_code(self):
        with (
            mock.patch(
                "afk_pr.__main__.submit", return_value={"action": {"state": "paused"}}
            ) as submit,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = main(["review", URL, "--action-id", "r1", "--expected-head", SHA])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["action"]["state"], "paused")
        self.assertEqual(submit.call_args.kwargs["action_id"], "r1")
        self.assertEqual(submit.call_args.kwargs["expected_head"], SHA)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch("afk_pr.__main__.submit") as submit,
        ):
            self.assertEqual(
                main(
                    [
                        "respond",
                        URL,
                        "--retry-publication",
                        "a" * 16,
                        "--action-id",
                        "r1",
                    ]
                ),
                1,
            )
            submit.assert_not_called()
