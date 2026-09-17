import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_inference import Capability, FixtureAdapter, ScriptedResult, invoke
from afk_pr import evaluation
from afk_pr.__main__ import main


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.name", "Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "README.md").write_text("Only read repository context.")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "base")
        self.sha = self.git(self.source, "rev-parse", "HEAD")
        self.host = self.root / "config.toml"
        self.host.write_text(
            f'schema_version=1\nstate_root="{self.root}/state"\n'
            f'beads_workspace="{self.root}"\n'
            '[projects.example]\nrepository="https://github.com/example/repo.git"\n'
        )
        self.bead = {
            "id": "central-example",
            "title": "Explain a command",
            "description": "Use EQEmu as example data, not as the owning repository.",
            "acceptance_criteria": "1. Preserve exact wording.\n   Multiline requirement.\n2. Verify.",
            "labels": ["project:example"],
            "status": "open",
            "notes": "An unresolved external prerequisite is documented here.",
            "dependencies": [
                {"id": "central-other", "status": "open", "dependency_type": "blocks"}
            ],
        }
        self.gh = mock.Mock()
        self.gh.api.side_effect = lambda endpoint: (
            {"default_branch": "main"}
            if endpoint == "repos/example/repo"
            else {"object": {"sha": self.sha}}
        )

    def git(self, path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def acquire(self, directory, job, phase):
        clone = Path(job["workspace_root"]) / job["id"] / phase
        clone.parent.mkdir(parents=True)
        self.git(self.root, "clone", str(self.source), str(clone))
        self.git(clone, "checkout", "--detach", job["head"])
        return clone

    def run_evaluation(
        self,
        report="Ready to attempt. The task has a clear outcome.",
        edit=False,
        acquisition_error=None,
    ):
        self.calls = []

        def inference(**kwargs):
            self.calls.append(kwargs)
            if edit:
                (Path(kwargs["execution_root"]) / "unexpected.txt").write_text(
                    "changed"
                )
            return invoke(
                **kwargs, adapter=FixtureAdapter((ScriptedResult(response=report),))
            )

        with (
            mock.patch("afk_run.read_bead", return_value=self.bead),
            mock.patch.object(
                evaluation.workspace,
                "acquire",
                side_effect=acquisition_error or self.acquire,
            ),
        ):
            return evaluation.evaluate(
                self.bead["id"], self.host, github=self.gh, inference=inference
            )

    def test_read_only_snapshot_and_pinned_context_without_fixture_policy(self):
        result = self.run_evaluation()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["context"]["commit"], self.sha)
        call = self.calls[0]
        self.assertEqual(call["requested_capability"], Capability.READ_ONLY)
        d = Path(result["directory"])
        frozen = json.loads((d / "bead.json").read_text())
        self.assertEqual(
            frozen["acceptance_criteria"], self.bead["acceptance_criteria"]
        )
        self.assertEqual(frozen["notes"], self.bead["notes"])
        self.assertEqual(frozen["dependencies"][0]["status"], "open")
        self.assertTrue((d / "inference/receipt.json").exists())
        self.assertEqual(self.git(call["execution_root"], "status", "--porcelain"), "")
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.sha)
        self.assertFalse(
            any(
                "data" in c.kwargs or "method" in c.kwargs
                for c in self.gh.api.call_args_list
            )
        )
        self.assertFalse((self.root / "state/pr-reviews").exists())

    def test_ambiguous_ownership_is_reportable_without_guessing_a_repository(self):
        self.bead["labels"].append("project:another")
        result = self.run_evaluation(
            "Needs clarification. Which project owns the work?"
        )
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["context"]["available"])
        self.gh.api.assert_not_called()
        self.assertEqual(self.calls[0]["requested_capability"], Capability.NO_TOOLS)
        self.assertEqual(
            self.calls[0]["untrusted_task_data"]["bead"]["labels"], self.bead["labels"]
        )

    def test_unregistered_project_and_unavailable_repository_remain_explicit(self):
        for error in (RuntimeError("offline"), subprocess.TimeoutExpired("git", 1)):
            with self.subTest(error=type(error).__name__):
                result = self.run_evaluation(acquisition_error=error)
                self.assertFalse(result["context"]["available"])
                self.assertEqual(result["state"], "completed")
                self.assertIn("unavailable_reason", result["context"])
        self.bead["labels"] = ["project:missing"]
        result = self.run_evaluation()
        self.assertIn(
            "no registered repository", result["context"]["unavailable_reason"]
        )

    def test_missing_bead_fails_before_inference_or_allocating_evidence(self):
        from afk_run import PreparationError

        with (
            mock.patch("afk_run.read_bead", side_effect=PreparationError("missing")),
            self.assertRaises(PreparationError),
        ):
            evaluation.evaluate("central-missing", self.host, github=self.gh)
        self.assertFalse((self.root / "state/evaluations").exists())

    def test_unexpected_changes_and_invalid_reports_are_not_success(self):
        result = self.run_evaluation(edit=True)
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["repository_unchanged"])
        self.assertFalse((Path(result["directory"]) / "report.md").exists())
        result = self.run_evaluation(report="")
        self.assertEqual(result["state"], "failed")

    def test_cli_dispatch_and_failure_exit(self):
        for state, code in (("completed", 0), ("failed", 1)):
            with mock.patch.object(
                evaluation, "evaluate", return_value={"state": state}
            ) as run:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            ["evaluate", "central-example", "--config", str(self.host)]
                        ),
                        code,
                    )
                run.assert_called_once_with("central-example", self.host)
                self.assertEqual(json.loads(output.getvalue())["state"], state)

    def test_credentials_only_reach_beads_subprocess(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets/dolt_beads_password.txt").write_text(
            "fixture-only-password\n"
        )
        with mock.patch("afk_run.read_bead", return_value=self.bead) as read:
            from afk_pr.beads import read_configured_bead
            from afk_pr.config import load_config

            read_configured_bead(self.bead["id"], load_config(self.host))
            self.assertEqual(
                read.call_args.kwargs["env"]["BEADS_DOLT_PASSWORD"],
                "fixture-only-password",
            )
        result = self.run_evaluation()
        for path in Path(result["directory"]).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"fixture-only-password", path.read_bytes())
