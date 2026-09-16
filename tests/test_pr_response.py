import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from afk_pr import jobs, response
from afk_pr.github import GitHub

URL = "https://github.com/example/repository/pull/12"


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.bare = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        jobs.git(self.repo, "config", "user.name", "Test")
        jobs.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "file.txt").write_text("before\n")
        jobs.git(self.repo, "add", ".")
        jobs.git(self.repo, "commit", "-qm", "initial")
        self.head = jobs.git(self.repo, "rev-parse", "HEAD")
        subprocess.run(["git", "init", "--bare", "-q", str(self.bare)], check=True)
        jobs.git(self.repo, "push", str(self.bare), "HEAD:refs/heads/repair")
        jobs.git(
            self.repo,
            "remote",
            "add",
            "origin",
            "https://github.com/example/repository.git",
        )
        self.directory = self.root / "jobs" / "1234567890abcdef"
        self.directory.mkdir(parents=True)
        self.job = {
            "id": self.directory.name,
            "pr_url": URL,
            "project": "test",
            "head": self.head,
            "base": self.head,
            "repository": str(self.repo),
            "kind": "response",
            "reviewers": [],
            "review_timeout": 20,
            "validation": {
                "command": ["true"],
                "evidence": "test",
                "timeout_seconds": 20,
            },
        }
        self.pr = {
            "state": "open",
            "head": {
                "sha": self.head,
                "ref": "repair",
                "repo": {"full_name": "example/repository"},
            },
            "base": {"sha": self.head, "ref": "main"},
        }
        self.context = {
            "pull_request": self.pr,
            "reviews": [
                {"body": "Please repair this", "user": {"login": "macroscope"}}
            ],
            "statuses": [{"state": "failure"}],
        }
        jobs.write(self.directory / "job.json", self.job)
        jobs.write(self.directory / "context.json", self.context)
        jobs.write(
            self.directory / "response.json",
            {"state": "queued", "publication": "pending"},
        )
        self.gh = mock.Mock(spec=GitHub)
        self.gh.api.side_effect = lambda *a, **k: copy.deepcopy(self.pr)
        self.gh.observe.return_value = self.context
        self.gh.comment.return_value = URL + "#response"
        self.gh.fixture_summary.return_value = URL + "#fixtures"
        self.launches = []
        self.pushes = []
        self.real_git = jobs.git

    def transport_git(self, repo, *args):
        if args[0] == "push":
            self.pushes.append(args)
            return self.real_git(repo, "push", str(self.bare), *args[2:])
        return self.real_git(repo, *args)

    def run_response(self, edit=None, *, launcher=None, outcome="succeeded"):
        def invoke(**kwargs):
            self.invocation = kwargs
            if edit:
                edit()
            return SimpleNamespace(
                outcome=outcome,
                value="Fixed useful feedback; declined an unrelated suggestion. @macroscope",
            )

        with (
            mock.patch.object(jobs, "checkout", return_value=self.repo),
            mock.patch.object(jobs, "git", side_effect=self.transport_git),
            mock.patch("afk_inference.runtime.invoke", side_effect=invoke),
        ):
            return response.respond(
                self.directory,
                self.job,
                github=self.gh,
                launcher=launcher or (lambda *a: self.launches.append(a)),
            )

    def edit(self):
        (self.repo / "file.txt").write_text("repaired\n")

    def test_repair_pushes_normal_commit_and_queues_exact_candidate(self):
        result = self.run_response(self.edit)
        self.assertEqual(result["state"], "completed")
        progress = jobs.read(self.directory / "response-progress.json")
        candidate = progress["candidate"]
        self.assertNotEqual(candidate, self.head)
        self.assertEqual(
            self.real_git(self.bare, "rev-parse", "refs/heads/repair"), candidate
        )
        self.assertEqual(self.real_git(self.repo, "rev-parse", "HEAD^"), self.head)
        self.assertEqual(progress["push"], "pushed")
        child = self.directory.parent / progress["fixture_job"]
        self.assertEqual(jobs.read(child / "job.json")["head"], candidate)
        self.assertEqual([call[1] for call in self.launches], ["fixtures"])
        self.assertFalse(any("force" in arg for arg in self.pushes[0]))
        self.assertEqual(self.invocation["purpose"], "feedback_response")
        self.assertEqual(self.invocation["requested_capability"].value, "WRITE")
        self.assertIn(
            "Do not classify every comment",
            self.invocation["trusted_task_instructions"],
        )
        self.assertEqual(
            jobs.read(self.directory / "context.json")["reviews"][0]["user"]["login"],
            "macroscope",
        )

    def test_no_change_disagreement_does_not_commit_push_or_run_fixtures(self):
        result = self.run_response()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.real_git(self.repo, "rev-parse", "HEAD"), self.head)
        self.assertFalse(self.pushes)
        self.assertFalse(self.launches)
        response.publish_response(self.directory, self.job, result, self.gh)
        body = self.gh.comment.call_args.args[1]
        self.assertIn("declined", body)
        self.assertIn("No repair commit", body)
        self.assertNotIn("@macroscope", body)

    def test_changed_head_before_start_avoids_inference(self):
        self.pr["head"]["sha"] = "b" * 40
        with mock.patch("afk_inference.runtime.invoke") as invoke:
            result = response.respond(self.directory, self.job, github=self.gh)
        self.assertEqual(result["state"], "paused")
        invoke.assert_not_called()

    def test_concurrent_head_base_or_closure_retains_repair_without_push(self):
        for change in ("head", "base", "closed"):
            with self.subTest(change=change):
                self.real_git(self.repo, "reset", "--hard", self.head)
                self.pr["head"]["sha"] = self.head
                self.pr["base"]["sha"] = self.head
                self.pr["state"] = "open"

                def edit(change=change):
                    self.edit()
                    if change == "closed":
                        self.pr["state"] = "closed"
                    else:
                        self.pr[change]["sha"] = "b" * 40

                result = self.run_response(edit)
                self.assertEqual(result["state"], "paused")
                self.assertNotEqual(
                    self.real_git(self.repo, "rev-parse", "HEAD"), self.head
                )
                self.assertFalse(self.pushes)
                self.assertFalse(self.launches)

    def test_regular_push_rejects_race_after_last_api_observation(self):
        def racing_git(repo, *args):
            if args[0] == "push":
                # A sibling commit appears after the final API observation.
                tree = self.real_git(self.repo, "rev-parse", self.head + "^{tree}")
                sibling = self.real_git(
                    self.repo, "commit-tree", tree, "-p", self.head, "-m", "concurrent"
                )
                self.real_git(
                    self.repo, "push", str(self.bare), sibling + ":refs/heads/repair"
                )
            return ResponseTests.transport_git(self, repo, *args)

        self.transport_git = racing_git
        with self.assertRaises(RuntimeError):
            self.run_response(self.edit)
        self.assertFalse(self.launches)
        self.assertEqual(
            jobs.read(self.directory / "response-progress.json")["push"], "attempted"
        )

    def test_model_commit_is_retained_but_not_pushed(self):
        def edit():
            self.edit()
            self.real_git(self.repo, "commit", "-am", "unexpected model commit")

        self.assertEqual(self.run_response(edit)["state"], "paused")
        self.assertFalse(self.pushes)

    def test_failed_inference_leaves_uncommitted_work_for_inspection(self):
        self.assertEqual(
            self.run_response(self.edit, outcome="failed")["state"], "failed"
        )
        self.assertEqual(self.real_git(self.repo, "rev-parse", "HEAD"), self.head)
        self.assertTrue(self.real_git(self.repo, "status", "--porcelain"))
        self.assertFalse(self.pushes)

    def test_fixture_launch_failure_preserves_pushed_candidate_and_failed_child(self):
        result = self.run_response(
            self.edit, launcher=mock.Mock(side_effect=OSError("no service"))
        )
        self.assertEqual(result["state"], "completed")
        progress = jobs.read(self.directory / "response-progress.json")
        child = self.directory.parent / progress["fixture_job"]
        self.assertEqual(jobs.read(child / "fixtures.json")["state"], "failed")
        self.assertEqual(progress["push"], "pushed")
        self.gh.fixture_status.assert_called()
        response.publish_response(self.directory, self.job, result, self.gh)
        self.assertIn(
            "Fixture job currently reports `failed`", self.gh.comment.call_args.args[1]
        )

    def test_summary_retry_does_not_repeat_model_push_or_fixtures(self):
        result = self.run_response(self.edit)
        jobs.write(self.directory / "response.json", result)
        self.gh.comment.side_effect = RuntimeError("offline")
        jobs.publish(self.directory, "response", github=self.gh)
        self.assertEqual(
            jobs.read(self.directory / "response.json")["publication"], "failed"
        )
        self.gh.comment.side_effect = None
        with (
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
            mock.patch.object(response, "respond") as run,
        ):
            jobs.retry_publication(self.directory)
        run.assert_not_called()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(
            jobs.read(self.directory / "response.json")["publication"], "published"
        )

    def test_submission_launches_only_response_and_rejects_forks(self):
        config = {
            "run_root": str(self.root / "runs"),
            "coordinator": {"agent_timeout_seconds": 20},
        }
        project = {"repository": str(self.repo), "validation": self.job["validation"]}
        launch = mock.Mock()
        with (
            mock.patch.object(jobs, "settings", return_value=(config, "test", project)),
            mock.patch.object(
                jobs, "status_job", side_effect=lambda d: jobs.read(d / "job.json")
            ),
        ):
            job = jobs.submit(
                URL, self.root / "config", respond=True, github=self.gh, launcher=launch
            )
            self.assertEqual(job["kind"], "response")
            self.assertEqual(launch.call_args.args[1], "response")
            self.gh.fixture_status.assert_not_called()
            self.pr["head"]["repo"]["full_name"] = "someone/fork"
            with self.assertRaisesRegex(ValueError, "branch in the PR repository"):
                jobs.submit(
                    URL,
                    self.root / "config",
                    respond=True,
                    github=self.gh,
                    launcher=launch,
                )
            self.assertEqual(launch.call_count, 1)

    def test_real_runtime_accepts_unstructured_response(self):
        from afk_inference.runtime import FixtureAdapter, ScriptedResult, invoke

        adapter = FixtureAdapter(
            script=(
                ScriptedResult(
                    response="No repair needed. The existing check covers this concern."
                ),
            )
        )
        with (
            mock.patch.object(jobs, "checkout", return_value=self.repo),
            mock.patch(
                "afk_inference.runtime.invoke",
                side_effect=lambda **kw: invoke(adapter=adapter, **kw),
            ),
        ):
            result = response.respond(self.directory, self.job, github=self.gh)
        self.assertEqual(result["state"], "completed")

    def test_response_marker_does_not_replace_fixture_comment(self):
        gh = GitHub()
        comments = [
            {"id": 1, "body": "<!-- afk-fixtures:123 -->", "user": {"login": "worker"}},
            {"id": 2, "body": "<!-- afk-response:123 -->", "user": {"login": "worker"}},
        ]
        with (
            mock.patch.object(gh, "collection", return_value=comments),
            mock.patch.object(
                gh, "api", side_effect=[{"login": "worker"}, {"html_url": URL}]
            ) as api,
        ):
            gh.comment({"id": "123", "pr_url": URL}, "response", "response")
        self.assertTrue(api.call_args.args[0].endswith("comments/2"))
        self.assertEqual(api.call_args.kwargs["method"], "PATCH")

    def test_worker_retains_result_when_fixture_pending_publication_fails(self):
        self.gh.fixture_status.side_effect = RuntimeError("offline")

        def invoke(**kwargs):
            self.edit()
            return SimpleNamespace(
                outcome="succeeded", value="Repaired the issue; validation is pending."
            )

        with (
            mock.patch.object(jobs, "checkout", return_value=self.repo),
            mock.patch.object(jobs, "git", side_effect=self.transport_git),
            mock.patch("afk_inference.runtime.invoke", side_effect=invoke),
            mock.patch.object(response, "GitHub", return_value=self.gh),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
        ):
            jobs.worker(self.directory, "response")
        result = jobs.read(self.directory / "response.json")
        progress = jobs.read(self.directory / "response-progress.json")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["publication"], "published")
        self.assertEqual(progress["push"], "pushed")
        child = self.directory.parent / progress["fixture_job"]
        self.assertEqual(jobs.read(child / "fixtures.json")["state"], "not_started")
        self.assertIn(progress["candidate"], self.gh.comment.call_args.args[1])

    def test_interrupted_response_retry_reports_uncertain_push_without_resuming(self):
        jobs.write(
            self.directory / "response-progress.json",
            {"candidate": self.head, "changed": True, "push": "attempted"},
        )
        with (
            mock.patch.object(
                jobs.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="inactive\n"),
            ),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
            mock.patch.object(response, "respond") as run,
        ):
            jobs.retry_publication(self.directory)
        run.assert_not_called()
        result = jobs.read(self.directory / "response.json")
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["publication"], "published")
        self.assertIn("Push: `attempted`", self.gh.comment.call_args.args[1])

    def test_response_publication_redacts_credentials_and_withholds_private_keys(self):
        self.run_response()
        result = {"state": "completed"}
        jobs.write(self.directory / "response.json", result)
        (self.directory / "response.md").write_text("password=never-publish")
        jobs.publish(self.directory, "response", github=self.gh)
        self.assertNotIn("never-publish", self.gh.comment.call_args.args[1])
        self.gh.comment.reset_mock()
        (self.directory / "response.md").write_text(
            "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----"
        )
        jobs.publish(self.directory, "response", github=self.gh)
        self.gh.comment.assert_not_called()
        self.assertEqual(
            jobs.read(self.directory / "response.json")["publication"], "failed"
        )

    def test_interruption_before_fixture_record_can_still_publish_summary(self):
        jobs.write(
            self.directory / "response-progress.json",
            {
                "candidate": self.head,
                "changed": True,
                "push": "pushed",
                "fixture_job": "fedcba0987654321",
            },
        )
        response.publish_response(
            self.directory, self.job, {"state": "interrupted"}, self.gh
        )
        self.assertIn(
            "Fixture job currently reports `not_started`",
            self.gh.comment.call_args.args[1],
        )
