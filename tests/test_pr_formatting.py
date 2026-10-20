import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_pr import formatting, jobs
from tests.formatting_fixture import FORMATTED, UNFORMATTED, policy


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "rust/actors").mkdir(parents=True)
        (self.repo / "rust/actors/Cargo.toml").write_text("[workspace]\n")
        (self.repo / "rust/actors/lib.rs").write_bytes(FORMATTED)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        jobs.git(self.repo, "config", "user.name", "Fixture")
        jobs.git(self.repo, "config", "user.email", "fixture@example.invalid")
        jobs.git(self.repo, "add", ".")
        jobs.git(self.repo, "commit", "-qm", "base")
        head = jobs.git(self.repo, "rev-parse", "HEAD")
        self.job = {
            "id": "1234567890abcdef",
            "base": head,
            "head": head,
            "formatter": policy(),
        }
        self.directory = self.root / "job"
        self.directory.mkdir()
        self.file = self.repo / "rust/actors/lib.rs"
        self.file.write_bytes(UNFORMATTED)

    def run_format(self, mode="normal"):
        self.job["formatter"] = policy(mode)
        return formatting.normalize(self.directory, self.job, self.repo, "creation")

    def test_formats_dirty_and_new_sources_without_touching_index_or_other_edits(self):
        new = self.repo / "rust/actors/new.rs"
        new.write_bytes(UNFORMATTED)
        unrelated = self.repo / "native.cpp"
        unrelated.write_text("keep my edit\n")
        index = (self.repo / ".git/index").read_bytes()
        self.assertIsNone(self.run_format())
        self.assertEqual(self.file.read_bytes(), FORMATTED)
        self.assertEqual(new.read_bytes(), FORMATTED)
        self.assertEqual(unrelated.read_text(), "keep my edit\n")
        self.assertEqual(index, (self.repo / ".git/index").read_bytes())
        receipt = jobs.read(self.directory / "creation-formatting/application.json")
        self.assertFalse(receipt["acceptance"])

    def test_noop_and_opt_out(self):
        self.file.write_bytes(FORMATTED)
        self.assertIsNone(self.run_format())
        self.assertEqual(
            jobs.read(self.directory / "creation-formatting/application.json")[
                "changed_files"
            ],
            [],
        )
        with mock.patch("afk_pr.formatting.run_command") as run:
            self.assertIsNone(
                formatting.normalize(self.directory, {}, self.repo, "response")
            )
        run.assert_not_called()

    def test_deletions_generated_files_configuration_and_modes_are_preserved(self):
        self.file.unlink()
        new = self.repo / "rust/actors/new.rs"
        new.write_bytes(UNFORMATTED)
        new.chmod(0o755)
        config = self.repo / "rustfmt.toml"
        config.write_text("edition = '2021'\n")
        generated = self.repo / "rust/actors/target/ignored.rs"
        generated.parent.mkdir()
        generated.write_bytes(UNFORMATTED)
        self.assertIsNone(self.run_format())
        self.assertFalse(self.file.exists())
        self.assertEqual(new.read_bytes(), FORMATTED)
        self.assertEqual(new.stat().st_mode & 0o777, 0o755)
        self.assertEqual(config.read_text(), "edition = '2021'\n")
        self.assertEqual(generated.read_bytes(), UNFORMATTED)

    def test_json_output_is_size_bounded_and_rejects_duplicate_keys(self):
        path = self.root / "result.json"
        path.write_text('{"version": 1, "version": 2}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            formatting.json_file(path)
        path.write_bytes(b" " * (formatting.JSON_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "output limit"):
            formatting.json_file(path)

    def test_bad_results_preserve_all_edits_and_index(self):
        for mode in (
            "fail",
            "malformed",
            "bad-path",
            "wrong-identity",
            "duplicate",
            "incomplete-cleanup",
            "rollback",
        ):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                self.job["formatter"] = policy(mode)
                index = (self.repo / ".git/index").read_bytes()
                result = formatting.normalize(
                    directory, self.job, self.repo, "response"
                )
                self.assertEqual(result["state"], "failed")
                self.assertEqual(self.file.read_bytes(), UNFORMATTED)
                self.assertEqual(index, (self.repo / ".git/index").read_bytes())

    def test_stale_output_preserves_newer_edit(self):
        self.assertEqual(self.run_format("stale")["state"], "failed")
        self.assertEqual(self.file.read_text(), "newer edit\n")

    def test_changed_tool_identity_stops_before_formatting(self):
        self.assertEqual(self.run_format("tool-drift")["state"], "failed")
        self.assertFalse(
            (self.directory / "creation-formatting/format.process.json").exists()
        )
        self.assertEqual(self.file.read_bytes(), UNFORMATTED)

    def test_symlink_source_is_refused(self):
        outside = self.root / "outside"
        outside.write_bytes(UNFORMATTED)
        self.file.unlink()
        self.file.symlink_to(outside)
        self.assertEqual(self.run_format()["state"], "failed")
        self.assertEqual(outside.read_bytes(), UNFORMATTED)

    def test_timeout_reaps_client_and_retains_work(self):
        self.job["formatter"] = {**policy("timeout"), "timeout_seconds": 1}
        self.assertEqual(
            formatting.normalize(self.directory, self.job, self.repo, "creation")[
                "state"
            ],
            "failed",
        )
        process = jobs.read(self.directory / "creation-formatting/format.process.json")
        self.assertTrue(process["timed_out"])
        self.assertEqual(self.file.read_bytes(), UNFORMATTED)

    def test_interrupted_invocation_does_not_apply_or_commit(self):
        with mock.patch("afk_pr.formatting.run_command") as run:

            def interrupted(command, workspace, timeout, stdout, stderr, **kwargs):
                stdout.write_text("")
                stderr.write_text("cancelled")
                return {
                    "exit_code": -15,
                    "error": None,
                    "timed_out": False,
                    "interrupted": True,
                }

            run.side_effect = interrupted
            self.assertEqual(self.run_format()["state"], "failed")
        self.assertEqual(self.file.read_bytes(), UNFORMATTED)

    def test_formatter_budget_and_cleanup_fit_creation_service(self):
        jobs.write(self.directory / "job.json", self.job)
        with mock.patch("afk_pr.jobs.subprocess.run") as launch:
            jobs.launch(self.directory, "creation", 20)
        command = launch.call_args.args[0]
        self.assertIn("--property=RuntimeMaxSec=1038", command)
        self.assertIn("--property=TimeoutStopSec=903", command)
