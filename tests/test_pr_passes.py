import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from afk_pr import jobs
from afk_pr.github import GitHub, identity

URL = "https://github.com/example/repository/pull/12"
SHA = "a" * 40


class FakeGitHub(GitHub):
    def __init__(self):
        self.posts = []
        self.comments = []
        self.fail_publication = False

    def observe(self, url):
        return {
            "pull_request": {
                "state": "open",
                "head": {"sha": SHA},
                "base": {"sha": "b" * 40},
            },
            "reviews": [{"user": {"login": "macroscopeapp[bot]"}, "body": "A concern"}],
        }

    def fixture_status(self, job, state, description):
        if self.fail_publication:
            raise RuntimeError("offline")
        self.posts.append((job["head"], state))

    def fixture_summary(self, job, body):
        self.comments.append(body)
        return URL + "#comment"

    def review(self, job, name, body):
        self.comments.append(body)
        return URL + "#review"


class ContextTests(unittest.TestCase):
    def test_pages_and_all_feedback_channels_are_retained(self):
        gh = GitHub()
        pr = {"head": {"sha": SHA}, "base": {"sha": "b" * 40}, "commits": 2}

        def api(endpoint, **kwargs):
            if endpoint.endswith("pulls/12"):
                return pr
            self.assertTrue(kwargs["pages"])
            if "/check-runs?" in endpoint:
                return [
                    {
                        "check_runs": [
                            {
                                "id": 1,
                                "name": "Macroscope",
                                "conclusion": "neutral",
                                "output": {"annotations_count": 1},
                            }
                        ]
                    }
                ]
            return [[{"body": "first"}], [{"body": "second"}]]

        with mock.patch.object(gh, "api", side_effect=api):
            context = gh.observe(URL)
        for name in ("comments", "reviews", "review_comments", "commits", "statuses"):
            self.assertEqual(len(context[name]), 2)
        self.assertEqual(len(context["checks"][0]["annotations"]), 2)

    def test_changed_head_and_incomplete_reads_fail(self):
        gh = GitHub()
        before = {"head": {"sha": SHA}, "base": {"sha": "b" * 40}}
        after = {"head": {"sha": "c" * 40}, "base": {"sha": "b" * 40}}
        with (
            mock.patch.object(gh, "api", side_effect=[before, after]),
            mock.patch.object(gh, "collection", return_value=[]),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            gh.observe(URL)
        with (
            mock.patch.object(gh, "api", return_value=before),
            mock.patch.object(
                gh, "collection", side_effect=RuntimeError("page failed")
            ),
            self.assertRaises(RuntimeError),
        ):
            gh.observe(URL)

    def test_invalid_urls_are_rejected(self):
        for url in (
            "https://other.example/a/b/pull/1",
            URL + "/../../issues",
            "--help",
        ):
            with self.assertRaises(ValueError):
                identity(url)

    def test_publication_recognizes_only_own_marker(self):
        gh = GitHub()
        job = {"id": "123", "pr_url": URL}
        calls = []

        def api(endpoint, **kw):
            calls.append((endpoint, kw))
            return {"login": "worker"} if endpoint == "user" else {"html_url": URL}

        comments = [
            {"id": 9, "body": "<!-- afk-fixtures:123 -->", "user": {"login": "worker"}}
        ]
        with (
            mock.patch.object(gh, "api", side_effect=api),
            mock.patch.object(gh, "collection", return_value=comments),
        ):
            gh.fixture_summary(job, "passed")
        self.assertEqual(calls[-1][1]["method"], "PATCH")
        self.assertIn("issues/comments/9", calls[-1][0])


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gh = FakeGitHub()
        self.project = {
            "repository": str(self.root / "repo"),
            "validation": {
                "command": [sys.executable, "-c", "print('fixture passed')"],
                "timeout_seconds": 10,
                "evidence": "test fixture",
            },
        }
        self.config = {
            "run_root": str(self.root),
            "coordinator": {"agent_timeout_seconds": 30},
        }

    def submit(self, fixtures_only=False, launcher=None):
        with (
            mock.patch.object(
                jobs, "settings", return_value=(self.config, "test", self.project)
            ),
            mock.patch.object(
                jobs, "status_job", side_effect=lambda d: {"directory": str(d)}
            ),
        ):
            result = jobs.submit(
                URL,
                self.root / "config",
                fixtures_only=fixtures_only,
                github=self.gh,
                launcher=launcher or (lambda *args: None),
            )
        return Path(result["directory"])

    def test_submit_returns_without_running_fixtures_or_inference(self):
        launches = []
        with (
            mock.patch.object(jobs, "fixtures") as fixtures,
            mock.patch.object(jobs, "review") as review,
        ):
            directory = self.submit(launcher=lambda *args: launches.append(args))
        fixtures.assert_not_called()
        review.assert_not_called()
        self.assertEqual([x[1] for x in launches], ["fixtures", "review"])
        self.assertEqual(jobs.read(directory / "job.json")["head"], SHA)
        self.assertEqual(self.gh.posts, [(SHA, "pending")])

    def test_fixtures_only_preserves_external_review_context(self):
        directory = self.submit(fixtures_only=True)
        self.assertFalse((directory / "review.json").exists())
        self.assertEqual(
            jobs.read(directory / "context.json")["reviews"][0]["user"]["login"],
            "macroscopeapp[bot]",
        )

    def test_pending_publication_failure_starts_nothing(self):
        self.gh.fail_publication = True
        launcher = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "not started"):
            self.submit(launcher=launcher)
        launcher.assert_not_called()

    def test_failed_service_start_is_reported(self):
        directory = self.submit(
            fixtures_only=True, launcher=mock.Mock(side_effect=OSError("no systemd"))
        )
        record = jobs.read(directory / "fixtures.json")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["publication"], "published")
        self.assertEqual(self.gh.posts[-1], (SHA, "error"))

    def test_real_fixture_runs_in_worker_and_retains_logs(self):
        directory = self.submit(fixtures_only=True)
        workspace = self.root / "fixture-workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        sha = jobs.git(workspace, "rev-parse", "HEAD")
        job = jobs.read(directory / "job.json")
        job["head"] = sha
        jobs.write(directory / "job.json", job)
        with (
            mock.patch.object(jobs, "checkout", return_value=workspace),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
        ):
            jobs.worker(directory, "fixtures")
        record = jobs.read(directory / "fixtures.json")
        self.assertEqual(record["state"], "passed")
        self.assertEqual(record["publication"], "published")
        self.assertIn("fixture passed", (directory / "fixtures.stdout.log").read_text())
        self.assertEqual(self.gh.posts[-1], (sha, "success"))

    def test_timeout_and_changed_candidate_cannot_pass(self):
        directory = self.submit(fixtures_only=True)
        job = jobs.read(directory / "job.json")
        for timed_out, head, expected in [
            (True, SHA, "timed_out"),
            (False, "c" * 40, "failed"),
        ]:
            with (
                mock.patch.object(jobs, "checkout", return_value=self.root),
                mock.patch.object(jobs, "git", side_effect=[head, ""]),
                mock.patch.object(
                    jobs,
                    "run_command",
                    return_value={
                        "exit_code": 0,
                        "error": None,
                        "timed_out": timed_out,
                    },
                ),
            ):
                self.assertEqual(jobs.fixtures(directory, job)["state"], expected)

    def test_publish_failure_retains_result_for_retry_without_work(self):
        directory = self.submit(fixtures_only=True)
        jobs.write(
            directory / "fixtures.json",
            {"state": "passed", "process": {"exit_code": 0}},
        )
        self.gh.fail_publication = True
        jobs.publish(directory, "fixtures", github=self.gh)
        self.assertEqual(
            jobs.read(directory / "fixtures.json")["publication"], "failed"
        )
        self.gh.fail_publication = False
        with mock.patch.object(jobs, "fixtures") as fixture:
            jobs.publish(directory, "fixtures", github=self.gh)
        fixture.assert_not_called()
        self.assertEqual(
            jobs.read(directory / "fixtures.json")["publication"], "published"
        )

    def test_status_detects_dead_worker_without_running_inference(self):
        directory = self.submit(fixtures_only=True)
        with mock.patch.object(
            jobs.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="inactive\n"),
        ):
            result = jobs.status_job(directory)
        self.assertEqual(result["phases"]["fixtures"]["state"], "interrupted")
        self.assertEqual(jobs.read(directory / "fixtures.json")["state"], "queued")

    def test_review_uses_real_runtime_fixture_without_waiting_for_tests(self):
        from afk_inference.runtime import FixtureAdapter, ScriptedResult, invoke

        directory = self.submit()
        adapter = FixtureAdapter(
            script=(ScriptedResult(response="A concrete review concern."),)
        )

        def fixture_invoke(**kwargs):
            return invoke(adapter=adapter, **kwargs)

        with (
            mock.patch.object(jobs, "checkout", return_value=self.root),
            mock.patch.object(jobs, "git", side_effect=[SHA, ""]),
            mock.patch("afk_inference.runtime.invoke", side_effect=fixture_invoke),
        ):
            result = jobs.review(directory, jobs.read(directory / "job.json"))
        self.assertEqual(result["state"], "completed")
        self.assertIn("concrete review", (directory / "review.md").read_text())
        self.assertEqual(jobs.read(directory / "fixtures.json")["state"], "queued")

    def test_launcher_uses_managed_service_not_blocking_wait(self):
        directory = self.submit()
        with mock.patch.object(jobs.subprocess, "run") as run:
            jobs.launch(directory, "fixtures", 30)
        command = run.call_args.args[0]
        self.assertIn("systemd-run", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertNotIn("--wait", command)

    def test_publication_retry_preserves_result_sealed_during_probe(self):
        directory = self.submit(fixtures_only=True)

        def finish_during_probe(*args, **kwargs):
            jobs.write(
                directory / "fixtures.json",
                {
                    "state": "passed",
                    "process": {"exit_code": 0},
                    "publication": "pending",
                },
            )
            return SimpleNamespace(returncode=0, stdout="inactive\n")

        with (
            mock.patch.object(jobs.subprocess, "run", side_effect=finish_during_probe),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
        ):
            jobs.retry_publication(directory)
        self.assertEqual(jobs.read(directory / "fixtures.json")["state"], "passed")
        self.assertEqual(self.gh.posts[-1], (SHA, "success"))

    def test_fixture_contention_uses_configured_wait_and_reports_not_executed(self):
        directory = self.submit(fixtures_only=True)
        job = jobs.read(directory / "job.json")
        job["validation"]["timeout_seconds"] = 3600
        with (
            mock.patch.object(
                jobs.fcntl, "flock", side_effect=[BlockingIOError(), None]
            ),
            mock.patch.object(jobs.time, "monotonic", side_effect=[0, 301]),
            mock.patch.object(jobs.time, "sleep") as sleep,
            jobs.acquire_fixture_slot(directory, job),
        ):
            pass
        sleep.assert_called_once_with(1)
        with (
            mock.patch.object(jobs, "fixtures", side_effect=TimeoutError()),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
        ):
            jobs.worker(directory, "fixtures")
        self.assertEqual(jobs.read(directory / "fixtures.json")["state"], "busy")
        self.assertIn("no fixtures executed", self.gh.comments[-1])

    def test_failure_diagnostics_are_bounded_redacted_and_published(self):
        directory = self.submit(fixtures_only=True)
        jobs.write(
            directory / "fixtures.json",
            {"state": "failed", "process": {"exit_code": 1}},
        )
        (directory / "fixtures.stderr.log").write_text(
            "x" * 10000
            + "\nFAILED test_zone_pressure\npassword=do-not-publish @macroscope-app <script>"
        )
        jobs.publish(directory, "fixtures", github=self.gh)
        body = self.gh.comments[-1]
        self.assertIn("FAILED test_zone_pressure", body)
        self.assertNotIn("do-not-publish", body)
        self.assertNotIn("@macroscope-app", body)
        self.assertNotIn("<script>", body)
        self.assertLess(len(body), 4500)

    def test_public_log_redaction_sees_headers_before_the_displayed_tail(self):
        directory = self.submit(fixtures_only=True)
        job = jobs.read(directory / "job.json")
        (directory / "fixtures.stderr.log").write_text(
            "-----BEGIN PRIVATE KEY-----\n"
            + "private-key-content\n" * 700
            + "-----END PRIVATE KEY-----"
        )
        excerpt = jobs.fixture_excerpt(directory, job)
        self.assertNotIn("private-key-content", excerpt)
        self.assertIn("withheld", excerpt)
        (directory / "fixtures.stderr.log").write_bytes(b"x" * (1024 * 1024 + 1))
        self.assertIn(
            "exceeds the publication limit", jobs.fixture_excerpt(directory, job)
        )

    def test_old_gh_pagination_decodes_all_json_documents(self):
        gh = GitHub()
        with mock.patch(
            "afk_pr.github.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0, stdout='[{"id":1}]\n[{"id":2}]\n'
            ),
        ) as run:
            result = gh.collection("repos/example/repository/issues/12/comments")
        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertNotIn("--slurp", run.call_args.args[0])

    def test_json_publication_uses_explicit_post_method(self):
        gh = GitHub()
        with mock.patch(
            "afk_pr.github.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="{}"),
        ) as run:
            gh.api("repos/example/repository/statuses/abc", data={"state": "pending"})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--method") + 1], "POST")
        self.assertIn('"state": "pending"', run.call_args.kwargs["input"])
