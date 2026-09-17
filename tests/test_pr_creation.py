import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from afk_pr import creation, jobs
from afk_pr.github import GitHub

URL = "https://github.com/example/repository/pull/12"


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.bare = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        jobs.git(self.repo, "config", "user.name", "Test")
        jobs.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "value.txt").write_text("before\n")
        jobs.git(self.repo, "add", ".")
        jobs.git(self.repo, "commit", "-qm", "initial")
        self.base = jobs.git(self.repo, "rev-parse", "HEAD")
        subprocess.run(["git", "init", "--bare", "-q", str(self.bare)], check=True)
        jobs.git(self.repo, "push", str(self.bare), "HEAD:refs/heads/main")
        self.remote = "https://github.com/example/repository.git"
        jobs.git(self.repo, "remote", "add", "origin", self.remote)
        self.bead = {
            "id": "central-example",
            "title": "Repair value",
            "description": "Implement one useful change",
            "acceptance_criteria": "value.txt contains after",
            "labels": ["project:example"],
            "status": "open",
        }
        self.beads = self.root / "beads"
        (self.beads / "secrets").mkdir(parents=True)
        (self.beads / "secrets/dolt_beads_password.txt").write_text("test-credential\n")
        self.project = {
            "repository": "https://github.com/example/repository.git",
            "base_ref": "origin/main",
            "validation": {
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('value.txt').read_text() == 'after\\n'; print('acceptance passed')",
                ],
                "timeout_seconds": 20,
                "evidence": "value fixture",
            },
        }
        self.config = {
            "workspace_root": self.root / "workspaces",
            "agent_timeout_seconds": 20,
            "acquisition_timeout_seconds": 20,
            "config_path": "fixture-host.toml",
            "run_root": self.root / "runs",
            "beads_workspace": str(self.beads),
            "coordinator": {"agent_timeout_seconds": 20},
            "projects": {"example": self.project},
        }
        self.gh = mock.Mock(spec=GitHub)
        self.gh.fixture_summary.return_value = URL + "#fixtures"
        self.gh.api.side_effect = self.api
        self.gh.collection.side_effect = lambda *a: copy.deepcopy(self.prs)
        self.prs = []
        self.posts = []
        self.launches = []
        self.pushes = []
        self.real_git = jobs.git
        acquisition = mock.patch("afk_pr.workspace.acquire", side_effect=self.acquire)
        acquisition.start()
        self.addCleanup(acquisition.stop)

    def api(self, path, **kwargs):
        if "data" not in kwargs:
            if path == "repos/example/repository":
                return {"default_branch": "main"}
            return {"object": {"sha": self.base}}
        payload = kwargs["data"]
        self.posts.append(payload)
        self.prs.append(
            {
                "html_url": URL,
                "state": "open",
                "body": payload["body"],
                "head": {
                    "ref": payload["head"],
                    "repo": {"full_name": "example/repository"},
                },
                "base": {"ref": payload["base"]},
            }
        )
        return self.prs[-1]

    def git(self, repo, *args):
        if args[0] == "fetch":
            args = tuple(str(self.bare) if arg == "origin" else arg for arg in args)
        if args[0] == "push":
            self.pushes.append(args)
        return self.real_git(
            repo, *(str(self.bare) if arg == self.remote else arg for arg in args)
        )

    def submit(self, **kwargs):
        with (
            mock.patch("afk_pr.creation.load_config", return_value=self.config),
            mock.patch("afk_run.read_bead", return_value=self.bead) as read,
            mock.patch.object(jobs, "git", side_effect=self.git),
            mock.patch(
                "afk_pr.creation.policy",
                return_value=(self.project["validation"], {"commit": self.base}, None),
            ),
            mock.patch(
                "afk_pr.config.policy",
                return_value=(self.project["validation"], {"commit": self.base}, None),
            ),
        ):
            result = creation.submit_creation(
                self.bead["id"],
                self.root / "config",
                github=self.gh,
                launcher=lambda *a: self.launches.append(a),
                **kwargs,
            )
            self.read_environment = read.call_args.kwargs["env"]
        return result

    def acquire(self, directory, job, phase):
        workspace = Path(job["workspace_root"]) / job["id"] / phase
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self.real_git(self.root, "clone", str(self.bare), str(workspace))
        self.real_git(workspace, "checkout", "--detach", job["head"])
        self.real_git(workspace, "config", "user.name", "Test")
        self.real_git(workspace, "config", "user.email", "test@example.com")
        return workspace

    def implement(
        self, directory, *, edit=True, before_return=None, outcome="succeeded"
    ):
        def invoke(**kwargs):
            self.invocation = kwargs
            if edit:
                (kwargs["execution_root"] / "value.txt").write_text("after\n")
            if before_return:
                before_return(kwargs)
            return SimpleNamespace(
                outcome=outcome,
                value="Implemented value repair. Configured validation is pending.",
            )

        with (
            mock.patch.object(jobs, "git", side_effect=self.git),
            mock.patch("afk_inference.runtime.invoke", side_effect=invoke),
            mock.patch("afk_pr.workspace.acquire", side_effect=self.acquire),
        ):
            return creation.implement(directory, jobs.read(directory / "job.json"))

    def publish(self, directory):
        with (
            mock.patch.object(jobs, "git", side_effect=self.git),
            mock.patch.object(
                jobs, "launch", side_effect=lambda *a: self.launches.append(a)
            ),
        ):
            jobs.publish(directory, "creation", github=self.gh)

    def prepared(self):
        submitted = self.submit()
        return Path(submitted["directory"])

    def test_submission_is_background_and_repeat_does_not_reimplement(self):
        result = self.submit()
        second = self.submit()
        self.assertEqual(result["job"]["id"], second["job"]["id"])
        self.assertEqual([x[1] for x in self.launches], ["creation"])
        self.assertEqual(result["job"]["base"], self.base)
        self.assertFalse(self.pushes)
        self.assertFalse(self.posts)
        self.assertEqual(
            self.read_environment["BEADS_DOLT_PASSWORD"], "test-credential"
        )
        for path in Path(result["directory"]).glob("*.json"):
            self.assertNotIn("test-credential", path.read_text())

    def test_initial_implementation_pushes_one_commit_creates_draft_and_queues_fixtures(
        self,
    ):
        directory = self.prepared()
        result = self.implement(directory)
        self.assertEqual(result["state"], "completed")
        jobs.write(directory / "creation.json", result)
        self.publish(directory)
        progress = jobs.read(directory / "creation-progress.json")
        candidate = progress["candidate"]
        job = jobs.read(directory / "job.json")
        self.assertEqual(
            self.real_git(self.bare, "rev-parse", "refs/heads/" + job["branch"]),
            candidate,
        )
        self.assertEqual(
            self.real_git(
                Path(jobs.read(directory / "job.json")["workspace_root"])
                / directory.name
                / "creation",
                "rev-parse",
                "HEAD^",
            ),
            self.base,
        )
        self.assertEqual((self.repo / "value.txt").read_text(), "before\n")
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0]["draft"])
        self.assertIn("value.txt contains after", self.posts[0]["body"])
        self.assertIn(self.bead["id"], self.posts[0]["body"])
        self.assertEqual(job["pr_url"], URL)
        status = jobs.status_job(directory, probe=False)
        self.assertEqual(
            status["phases"]["creation"]["progress"]["candidate"], candidate
        )
        child = jobs.read(directory.parent / progress["fixture_job"] / "job.json")
        self.assertEqual(child["head"], candidate)
        self.assertEqual(child["creation_job"], job["id"])
        self.assertEqual([x[1] for x in self.launches], ["creation", "fixtures"])
        self.assertEqual(self.invocation["purpose"], "attempt")
        self.assertIn(
            "--force-with-lease=refs/heads/afk-pr-central-example:", self.pushes[0]
        )
        self.assertNotIn("--force", self.pushes[0])
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "published"
        )

    def test_no_change_has_local_explanation_without_empty_commit_or_pr(self):
        directory = self.prepared()
        result = self.implement(directory, edit=False)
        jobs.write(directory / "creation.json", result)
        self.publish(directory)
        self.assertEqual(result["state"], "paused")
        self.assertTrue((directory / "creation.md").exists())
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "not_applicable"
        )
        self.assertFalse(self.posts)
        self.assertFalse(self.pushes)

    def test_remote_base_move_retains_repair_without_push(self):
        directory = self.prepared()

        def concurrent(_):
            self.real_git(self.repo, "commit", "--allow-empty", "-qm", "concurrent")
            self.real_git(self.repo, "push", str(self.bare), "HEAD:refs/heads/main")

        result = self.implement(directory, before_return=concurrent)
        self.assertEqual(result["state"], "paused")
        self.assertFalse(self.pushes)
        self.assertEqual(
            jobs.read(directory / "creation-progress.json")["push"], "not_started"
        )

    def test_model_commit_and_failed_inference_are_never_pushed(self):
        directory = self.prepared()

        def commit(kwargs):
            self.real_git(kwargs["execution_root"], "commit", "-am", "model commit")

        result = self.implement(directory, before_return=commit)
        self.assertEqual(result["state"], "paused")
        self.assertFalse(self.pushes)

    def test_existing_remote_branch_rejects_before_launch(self):
        self.real_git(
            self.repo, "push", str(self.bare), "HEAD:refs/heads/afk-pr-central-example"
        )
        with self.assertRaisesRegex(ValueError, "branch already exists"):
            self.submit()
        self.assertFalse(self.launches)

    def test_closed_bead_is_rejected_before_launch(self):
        self.bead["status"] = "closed"
        with self.assertRaisesRegex(ValueError, "closed Bead"):
            self.submit()
        self.assertFalse(self.launches)

    def test_pr_created_but_reply_lost_is_recovered_without_duplicate(self):
        directory = self.prepared()
        jobs.write(directory / "creation.json", self.implement(directory))

        def lost(path, **kwargs):
            result = self.api(path, **kwargs)
            if "data" in kwargs:
                raise RuntimeError("response lost after PR creation")
            return result

        self.gh.api.side_effect = lost
        self.publish(directory)
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "failed"
        )
        self.gh.api.side_effect = self.api
        self.publish(directory)
        self.publish(directory)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual([x[1] for x in self.launches], ["creation", "fixtures"])
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "published"
        )

    def test_interrupted_push_can_publish_only_if_remote_matches_candidate(self):
        directory = self.prepared()
        self.implement(directory)
        progress = jobs.read(directory / "creation-progress.json")
        progress["push"] = "attempted"
        jobs.write(directory / "creation-progress.json", progress)
        jobs.write(directory / "creation.json", {"state": "interrupted"})
        self.publish(directory)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(jobs.read(directory / "creation.json")["state"], "interrupted")

    def test_remote_move_before_publication_blocks_new_pr(self):
        directory = self.prepared()
        jobs.write(directory / "creation.json", self.implement(directory))
        workspace = (
            Path(jobs.read(directory / "job.json")["workspace_root"])
            / directory.name
            / "creation"
        )
        self.real_git(workspace, "commit", "--allow-empty", "-qm", "another actor")
        self.real_git(
            workspace, "push", str(self.bare), "HEAD:refs/heads/afk-pr-central-example"
        )
        self.publish(directory)
        self.assertFalse(self.posts)
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "failed"
        )

    def test_fixture_failure_is_not_reexecuted_by_publication_retry(self):
        directory = self.prepared()
        jobs.write(directory / "creation.json", self.implement(directory))
        self.gh.fixture_status.side_effect = RuntimeError("offline")
        self.publish(directory)
        self.assertEqual(
            jobs.read(directory / "creation.json")["publication"], "failed"
        )
        progress = jobs.read(directory / "creation-progress.json")
        child = directory.parent / progress["fixture_job"]
        self.assertEqual(jobs.read(child / "fixtures.json")["state"], "not_started")
        self.gh.fixture_status.side_effect = None
        self.publish(directory)
        self.assertEqual(jobs.read(child / "fixtures.json")["state"], "not_started")
        self.assertEqual([x[1] for x in self.launches], ["creation"])

    def test_existing_pr_is_reported_without_new_local_job(self):
        self.prs = [
            {
                "html_url": URL,
                "state": "closed",
                "body": "<!-- afk-bead:central-example -->",
                "head": {
                    "ref": "afk-pr-central-example",
                    "repo": {"full_name": "example/repository"},
                },
                "base": {"ref": "main"},
            }
        ]
        result = self.submit()
        self.assertEqual(result["pr_url"], URL)
        self.assertEqual(result["state"], "closed")
        self.assertFalse(self.launches)
        self.assertFalse(self.posts)

    def test_unowned_existing_pr_is_not_modified_or_duplicated(self):
        self.prs = [{"html_url": URL, "state": "open", "body": "Someone else's work"}]
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.submit()
        self.assertFalse(self.posts)

    def test_ambiguous_project_label_is_rejected(self):
        self.bead["labels"].append("project:other")
        from afk_run import PreparationError

        with self.assertRaisesRegex(PreparationError, "exactly one"):
            self.submit()

    def test_real_runtime_fixture_can_pause_without_changes(self):
        from afk_inference.runtime import FixtureAdapter, ScriptedResult, invoke

        directory = self.prepared()
        adapter = FixtureAdapter(
            script=(ScriptedResult(response="Need clarification; no edits."),)
        )
        with (
            mock.patch.object(jobs, "git", side_effect=self.git),
            mock.patch(
                "afk_inference.runtime.invoke",
                side_effect=lambda **kw: invoke(adapter=adapter, **kw),
            ),
        ):
            result = creation.implement(directory, jobs.read(directory / "job.json"))
        self.assertEqual(result["state"], "paused")
        self.assertFalse(self.pushes)

    def test_fixture_worker_tests_published_candidate_after_pr_advances(self):
        directory = self.prepared()
        jobs.write(directory / "creation.json", self.implement(directory))
        self.publish(directory)
        progress = jobs.read(directory / "creation-progress.json")
        workspace = (
            Path(jobs.read(directory / "job.json")["workspace_root"])
            / directory.name
            / "creation"
        )
        (workspace / "value.txt").write_text("later unrelated change\n")
        self.real_git(workspace, "commit", "-am", "later")
        self.real_git(workspace, "push", str(self.bare), "HEAD:refs/pull/12/head")
        child = directory.parent / progress["fixture_job"]
        with (
            mock.patch.object(jobs, "git", side_effect=self.git),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
        ):
            jobs.worker(child, "fixtures")
        self.assertEqual(jobs.read(child / "fixtures.json")["state"], "passed")
        self.assertEqual(
            self.gh.fixture_status.call_args.args[0]["head"], progress["candidate"]
        )
        self.assertEqual(self.gh.fixture_status.call_args.args[1], "success")
        self.assertIn("acceptance passed", (child / "fixtures.stdout.log").read_text())

    def test_create_only_push_rejects_branch_created_after_last_observation(self):
        directory = self.prepared()
        transport = self.git

        def concurrent(repo, *args):
            if args[0] == "push":
                self.real_git(
                    self.repo,
                    "push",
                    str(self.bare),
                    "HEAD:refs/heads/afk-pr-central-example",
                )
            return transport(repo, *args)

        self.git = concurrent
        with self.assertRaises(RuntimeError):
            self.implement(directory)
        self.assertEqual(
            self.real_git(self.bare, "rev-parse", "refs/heads/afk-pr-central-example"),
            self.base,
        )
        self.assertEqual(
            jobs.read(directory / "creation-progress.json")["push"], "attempted"
        )
        self.assertFalse(self.posts)
