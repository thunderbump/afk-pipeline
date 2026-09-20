"""Real clone deletion with retention, worker, publication and retry boundaries."""

import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_pr import garbage_collection as gc
from afk_pr import jobs
from afk_pr.__main__ import main


class GarbageCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "run_root": self.root / "state",
            "workspace_root": self.root / "workspaces",
            "projects": {"test": {}},
        }
        self.jobs = self.config["run_root"] / "pr-reviews"
        self.jobs.mkdir(parents=True)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q")
        (self.source / "code").write_text("original")
        self.git(self.source, "add", ".")
        self.git(
            self.source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "original",
        )
        self.head = self.git(self.source, "rev-parse", "HEAD")
        self.patcher = mock.patch.object(gc.lifecycle, "quiescent", return_value=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def git(self, path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def fixture(self, number, state="passed"):
        identifier = f"{number:016x}"
        directory = self.jobs / identifier
        directory.mkdir()
        job = {
            "id": identifier,
            "layout": "independent-clones-v1",
            "project": "test",
            "kind": "review",
            "pr_url": "https://github.com/example/test/pull/1",
            "created_at": f"2026-09-{number:02}T00:00:00Z",
            "workspace_root": str(self.config["workspace_root"]),
            "expected_phases": ["fixtures"],
            "head": self.head,
            "cleanup_allowed": True,
        }
        jobs.write(directory / "job.json", job)
        jobs.write(
            directory / "fixtures.json", {"state": state, "publication": "published"}
        )
        for name in ["fixtures.stdout.log", "fixtures.stderr.log"]:
            (directory / name).write_text("diagnostics")
        workspace = self.config["workspace_root"] / identifier / "fixtures"
        subprocess.run(
            ["git", "clone", "-q", str(self.source), str(workspace)], check=True
        )
        return directory, workspace

    def result(self, report, number=1):
        return next(r for r in report["jobs"] if r["job_id"] == f"{number:016x}")

    def test_preview_apply_and_repeated_apply_preserve_evidence(self):
        directory, workspace = self.fixture(1)
        self.fixture(2)
        self.fixture(3)
        preview = gc.collect(self.config, "test")
        self.assertEqual(self.result(preview)["outcome"], "eligible")
        self.assertGreater(preview["bytes_eligible"], 0)
        self.assertTrue(workspace.exists())
        self.assertFalse((directory / "cleanup.json").exists())
        report = gc.collect(self.config, "test", apply=True)
        self.assertEqual(self.result(report)["outcome"], "removed")
        self.assertFalse(workspace.exists())
        self.assertEqual((directory / "fixtures.stdout.log").read_text(), "diagnostics")
        self.assertEqual(
            gc.collect(self.config, "test", apply=True)["bytes_removed"], 0
        )

    def test_keep_latest_failed_and_successful_even_outside_recent_window(self):
        self.fixture(1, "failed")
        self.fixture(2)
        self.fixture(3)
        self.fixture(4, "running")
        self.fixture(5, "running")
        report = gc.collect(self.config, "test", apply=True)
        for number in [1, 3, 4, 5]:
            self.assertEqual(self.result(report, number)["outcome"], "retained")
        self.assertEqual(self.result(report, 2)["outcome"], "removed")

    def test_active_dirty_and_unpublished_are_retained(self):
        directory, workspace = self.fixture(1)
        self.fixture(2)
        self.fixture(3)
        with mock.patch.object(gc.lifecycle, "quiescent", return_value=False):
            self.assertIn(
                "inactivity",
                self.result(gc.collect(self.config, "test", apply=True))["reason"],
            )
        with (directory / "lifecycle.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            self.assertIn(
                "active",
                self.result(gc.collect(self.config, "test", apply=True))["reason"],
            )
        (workspace / "new-code").write_text("unpublished")
        self.assertIn(
            "unpublished",
            self.result(gc.collect(self.config, "test", apply=True))["reason"],
        )
        (workspace / "new-code").unlink()
        jobs.write(
            directory / "fixtures.json", {"state": "passed", "publication": "failed"}
        )
        self.assertIn(
            "published",
            self.result(gc.collect(self.config, "test", apply=True))["reason"],
        )
        self.assertTrue(workspace.exists())

    def test_external_without_adapter_and_changed_root_are_retained(self):
        directory, workspace = self.fixture(1)
        self.fixture(2)
        self.fixture(3)
        job = jobs.read(directory / "job.json")
        resource = {
            "name": "validation",
            "worker_home": str(self.root / "worker"),
            "stack_path": str(self.root / "stack"),
        }
        job["fixture_resource"] = resource
        self.config["fixture_resources"] = {"validation": resource}
        jobs.write(directory / "job.json", job)
        self.assertIn(
            "adapter",
            self.result(gc.collect(self.config, "test", apply=True))["reason"],
        )
        job["workspace_root"] = str(self.root / "unowned")
        jobs.write(directory / "job.json", job)
        self.assertIn(
            "root", self.result(gc.collect(self.config, "test", apply=True))["reason"]
        )
        self.assertTrue(workspace.exists())

    def test_failed_old_fixture_can_be_removed_and_interruption_resumes(self):
        directory, workspace = self.fixture(1, "failed")
        self.fixture(2, "failed")
        self.fixture(3)
        with mock.patch.object(gc.shutil, "rmtree", side_effect=OSError("interrupted")):
            self.assertEqual(
                self.result(gc.collect(self.config, "test", apply=True))["outcome"],
                "retained",
            )
        self.assertEqual(jobs.read(directory / "cleanup.json")["state"], "deleting")
        self.assertEqual(
            self.result(gc.collect(self.config, "test", apply=True))["outcome"],
            "removed",
        )
        self.assertFalse(workspace.exists())

    def test_symlink_and_linked_worktree_are_retained(self):
        _directory, workspace = self.fixture(1)
        self.fixture(2)
        self.fixture(3)
        original = workspace.with_name("saved")
        workspace.rename(original)
        workspace.symlink_to(original, target_is_directory=True)
        self.assertEqual(
            self.result(gc.collect(self.config, "test", apply=True))["outcome"],
            "retained",
        )
        workspace.unlink()
        self.git(self.source, "worktree", "add", "--detach", str(workspace), self.head)
        self.assertIn(
            "linked", self.result(gc.collect(self.config, "test", apply=True))["reason"]
        )
        self.assertTrue(original.exists())

    def test_pushed_candidate_required_and_latest_response_protected(self):
        directory, workspace = self.fixture(1)
        job = jobs.read(directory / "job.json")
        job.update(kind="response", expected_phases=["response"])
        response = workspace.with_name("response")
        workspace.rename(response)
        jobs.write(directory / "job.json", job)
        jobs.write(
            directory / "response.json",
            {"state": "completed", "publication": "published"},
        )
        (directory / "response.md").write_text("implemented")
        (directory / "inference").mkdir()
        jobs.write(directory / "inference/receipt.json", {})
        jobs.write(
            directory / "response-progress.json",
            {"candidate": self.head, "changed": True, "push": "pending"},
        )
        with self.assertRaisesRegex(gc.Retain, "push"):
            gc.collect_job(self.config, directory, job, True)
        jobs.write(
            directory / "response-progress.json",
            {"candidate": self.head, "changed": True, "push": "pushed"},
        )
        failed, _ = self.fixture(2)
        newer = jobs.read(failed / "job.json")
        newer.update(kind="response", expected_phases=["response"])
        jobs.write(failed / "job.json", newer)
        jobs.write(
            failed / "response.json", {"state": "failed", "publication": "published"}
        )
        self.fixture(3)
        self.assertEqual(
            self.result(gc.collect(self.config, "test", apply=True))["outcome"],
            "retained",
        )
        self.assertTrue(response.exists())

    def test_adapter_lock_covers_actual_removal(self):
        directory, workspace = self.fixture(1)
        job = jobs.read(directory / "job.json")
        from contextlib import contextmanager

        held = []

        @contextmanager
        def adapter(*args):
            held.append(True)
            try:
                yield []
            finally:
                held.pop()

        remove = gc.shutil.rmtree

        def checked_remove(path):
            self.assertEqual(held, [True])
            remove(path)

        with (
            mock.patch.object(gc, "resource_targets", adapter),
            mock.patch.object(gc.shutil, "rmtree", side_effect=checked_remove),
        ):
            gc.collect_job(self.config, directory, job, True)
        self.assertEqual(held, [])
        self.assertFalse(workspace.exists())

    def test_cli_defaults_to_preview(self):
        with (
            mock.patch("afk_pr.config.load_config", return_value=self.config),
            mock.patch.object(gc, "collect", return_value={}) as collect,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["gc", "--project", "test"]), 0)
            collect.assert_called_once_with(self.config, "test", keep=2, apply=False)
        with self.assertRaises(ValueError):
            gc.collect(self.config, "test", keep=0)


if __name__ == "__main__":
    unittest.main()
