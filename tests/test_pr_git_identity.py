import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from afk_pr import creation, jobs, response


class GitIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "clone"
        self.directory = self.root / "job"
        self.directory.mkdir()
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") and k != "EMAIL"
        }
        environment.update(
            HOME=str(self.root),
            XDG_CONFIG_HOME=str(self.root / "config"),
            GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
        )
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        jobs.git(self.repo, "config", "user.useConfigOnly", "true")
        jobs.git(
            self.repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        )
        self.head = jobs.git(self.repo, "rev-parse", "HEAD")

    def test_missing_identity_stops_both_write_paths_before_inference(self):
        with (
            patch.object(jobs, "checkout", return_value=self.repo),
            patch("afk_inference.runtime.invoke") as inference,
            patch.object(creation, "remote_head", side_effect=[None, self.head]),
        ):
            result = creation.implement(
                self.directory,
                {"branch": "repair", "base_branch": "main", "base": self.head},
            )
            self.assertEqual(result["state"], "failed")
            self.assertIn("git config --global", result["reason"])
            inference.assert_not_called()
        jobs.write(self.directory / "context.json", {"pull_request": {}})
        with (
            patch.object(jobs, "checkout", return_value=self.repo),
            patch.object(response, "response_branch", return_value="repair"),
            patch.object(response, "unchanged", return_value=True),
            patch("afk_inference.runtime.invoke") as inference,
        ):
            result = response.respond(
                self.directory, {"pr_url": "https://github.com/example/repo/pull/1"}
            )
            self.assertEqual(result["state"], "failed")
            inference.assert_not_called()
        self.assertTrue((self.directory / "creation.git.log").read_text())
        self.assertEqual(
            (self.directory / "response.git.log").stat().st_mode & 0o777, 0o600
        )

    def test_author_override_does_not_hide_missing_committer(self):
        with patch.dict(
            os.environ,
            {"GIT_AUTHOR_NAME": "Author", "GIT_AUTHOR_EMAIL": "author@example.invalid"},
        ):
            result = jobs.check_commit_identity(self.directory, self.repo, "response")
        self.assertIn("GIT_COMMITTER_IDENT", result["reason"])

    def test_global_identity_allows_commit_in_fresh_clone(self):
        subprocess.run(
            ["git", "config", "--global", "user.name", "Host identity"], check=True
        )
        subprocess.run(
            ["git", "config", "--global", "user.email", "host@example.invalid"],
            check=True,
        )
        clone = self.root / "independent"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True)
        self.assertIsNone(jobs.check_commit_identity(self.directory, clone, "creation"))
        jobs.git(clone, "commit", "--allow-empty", "-qm", "identity proof")
        self.assertEqual(
            jobs.git(clone, "log", "-1", "--format=%an <%ae>"),
            "Host identity <host@example.invalid>",
        )
        self.assertFalse((self.directory / "creation.git.log").exists())

    def test_read_only_review_does_not_require_commit_identity(self):
        jobs.write(self.directory / "context.json", {})
        with (
            patch.object(jobs, "checkout", return_value=self.repo),
            patch(
                "afk_inference.runtime.invoke",
                return_value=SimpleNamespace(outcome="succeeded", value="No concerns"),
            ) as inference,
        ):
            result = jobs.review(
                self.directory,
                {"head": self.head, "base": self.head, "review_timeout": 5},
            )
        self.assertEqual(result["state"], "completed")
        inference.assert_called_once()

    def test_git_stderr_is_private_and_not_in_worker_publication(self):
        private = (
            "Git hook rejected commit; private credential sentinel-not-a-real-secret"
        )
        with (
            patch.object(
                jobs.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", private),
            ),
            self.assertRaises(jobs.GitFailure) as caught,
        ):
            jobs.git(self.repo, "commit", "-m", "probe")
        self.assertNotIn(private, str(caught.exception))
        jobs.write(self.directory / "job.json", {})
        jobs.write(self.directory / "creation.json", {"state": "queued"})
        with (
            patch.object(creation, "implement", side_effect=caught.exception),
            patch.object(jobs, "publish") as publish,
        ):
            jobs.worker(self.directory, "creation")
        self.assertEqual((self.directory / "creation.git.log").read_text(), private)
        self.assertNotIn(private, (self.directory / "creation.json").read_text())
        self.assertNotIn(private, (self.directory / "creation.error.log").read_text())
        publish.assert_called_once_with(self.directory, "creation")


if __name__ == "__main__":
    unittest.main()
