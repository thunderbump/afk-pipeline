"""Exercise the real TOML resolver, clone acquisition and cleanup seam."""

import base64
import contextlib
import fcntl
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_pr import config, creation, jobs, lifecycle, workspace
from afk_pr.__main__ import main

URL = "https://github.com/example/repository/pull/1"
REMOTE = "https://github.com/example/repository.git"


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.run = subprocess.run
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / "value").write_text("base")
        (self.source / "afk.toml").write_text(
            'schema_version = 1\n[fixtures]\ncommand = ["sh", "-c", "test -f value"]\n'
        )
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "base")
        self.base = self.git(self.source, "rev-parse", "HEAD")
        (self.source / "afk.toml").write_text(
            'schema_version = 1\n[fixtures]\ncommand = ["false"]\n'
        )
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "candidate changes policy")
        self.head = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "update-ref", "refs/pull/1/head", self.head)
        self.host = self.root / "host.toml"
        self.host.write_text(
            f'schema_version = 1\nstate_root = "{self.root}/state"\n[projects.example]\nrepository = "{REMOTE}"\n'
        )
        self.gh = mock.Mock()
        self.pr = {
            "state": "open",
            "head": {
                "sha": self.head,
                "ref": "feature",
                "repo": {"full_name": "example/repository"},
            },
            "base": {"sha": self.base, "ref": "main"},
        }
        self.gh.observe.return_value = {"pull_request": self.pr}
        self.gh.api.side_effect = self.api
        self.gh.fixture_summary.return_value = URL + "#fixture"
        self.gh.collection.return_value = []

    def git(self, repo, *args):
        p = self.run(
            ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
        )
        return p.stdout.strip()

    def api(self, endpoint, **kwargs):
        prefix = "repos/example/repository/"
        if endpoint == "repos/example/repository":
            return {"default_branch": "main"}
        if endpoint.startswith(prefix + "git/ref/heads/"):
            return {"object": {"sha": self.base}}
        if endpoint.startswith(prefix + "git/trees/"):
            sha = endpoint.rsplit("/", 1)[1]
            entries = []
            for line in self.git(self.source, "ls-tree", sha).splitlines():
                meta, name = line.split("\t")
                mode, kind, obj = meta.split()
                entries.append({"mode": mode, "type": kind, "sha": obj, "path": name})
            return {"tree": entries}
        if endpoint.startswith(prefix + "git/blobs/"):
            data = self.git(
                self.source, "cat-file", "blob", endpoint.rsplit("/", 1)[1]
            ).encode()
            return {
                "encoding": "base64",
                "size": len(data),
                "content": base64.b64encode(data).decode(),
            }
        raise AssertionError(endpoint)

    def transport(self, command, **kwargs):
        if command[0] == "git":
            command = [
                "git",
                "-c",
                f"url.{self.source.as_uri()}.insteadOf={REMOTE}",
                *command[1:],
            ]
        return self.run(command, **kwargs)

    def submit(self, **kwargs):
        result = jobs.submit(
            URL, self.host, github=self.gh, launcher=lambda *a: None, **kwargs
        )
        return Path(result["directory"])

    def test_real_toml_submission_has_no_beads_or_checkout_and_snapshots_base_policy(
        self,
    ):
        d = self.submit(fixtures_only=True)
        job = jobs.read(d / "job.json")
        self.assertEqual(job["validation"]["command"], ["sh", "-c", "test -f value"])
        self.assertEqual(job["policy"]["commit"], self.base)
        self.assertEqual(job["repository"], REMOTE)
        self.host.write_text("invalid TOML")
        with mock.patch("afk_pr.workspace.subprocess.run", side_effect=self.transport):
            a = jobs.checkout(d, job, "fixtures")
            b = jobs.checkout(d, job, "review")
        self.assertEqual(self.git(a, "rev-parse", "HEAD"), self.head)
        (a / "value").write_text("private")
        self.git(a, "config", "test.private", "yes")
        self.assertEqual(self.git(b, "status", "--porcelain"), "")
        self.assertFalse((a / ".git/objects/info/alternates").exists())

    def test_exact_old_head_survives_moved_ref_and_unavailable_sha_fails(self):
        d = self.submit(fixtures_only=True)
        job = jobs.read(d / "job.json")
        job["head"] = self.base
        with mock.patch("afk_pr.workspace.subprocess.run", side_effect=self.transport):
            a = workspace.acquire(d, job, "fixtures")
            self.assertEqual(self.git(a, "rev-parse", "HEAD"), self.base)
            job["head"] = "f" * 40
            with self.assertRaisesRegex(ValueError, "pinned_commit_unavailable"):
                workspace.acquire(d, job, "review")

    def test_unknown_duplicate_and_old_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "repository_not_registered"):
            config.settings(self.host, "https://github.com/other/repo/pull/2")
        self.host.write_text(
            self.host.read_text() + f'\n[projects.duplicate]\nrepository="{REMOTE}"\n'
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            config.load_config(self.host)
        old = self.root / "old.json"
        old.write_text(json.dumps({"run_root": str(self.root)}))
        with self.assertRaisesRegex(ValueError, "Legacy JSON"):
            config.load_config(old)
        self.assertEqual(
            config.load_config(old, historical=True)["run_root"], self.root
        )

    def test_repo_fallback_requires_committed_executable_and_override_is_visible(self):
        self.git(self.source, "rm", "afk.toml")
        self.git(self.source, "commit", "-m", "missing policy")
        sha = self.git(self.source, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "fixture_policy_missing"):
            config.policy(self.gh, "example/repository", sha, {})
        validation, origin, _ = config.policy(
            self.gh,
            "example/repository",
            sha,
            {"fixtures": {"command": ["true"], "timeout_seconds": 2700}},
        )
        self.assertEqual(origin["source"], "host_override")
        self.assertEqual(validation["timeout_seconds"], 2700)
        (self.source / "scripts").mkdir()
        (self.source / "scripts/validate").write_text("#!/bin/sh\nexit 0\n")
        (self.source / "scripts/validate").chmod(0o755)
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "entrypoint")
        sha = self.git(self.source, "rev-parse", "HEAD")
        self.assertEqual(
            config.policy(self.gh, "example/repository", sha, {})[1]["source"],
            "conventional_entrypoint",
        )

    def test_cli_review_with_default_host_and_real_resolver(self):
        with (
            mock.patch("afk_pr.config.DEFAULT_CONFIG", self.host),
            mock.patch.object(jobs, "GitHub", return_value=self.gh),
            mock.patch.object(
                jobs.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="active\n"),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(["review", URL]), 0)
        self.assertEqual(
            json.loads(output.getvalue())["job"]["layout"], "independent-clones-v1"
        )

    def test_creation_uses_bead_fields_and_remote_default_without_checkout(self):
        beads = self.root / "beads"
        beads.mkdir()
        self.host.write_text(
            self.host.read_text().replace(
                "schema_version = 1", f'schema_version = 1\nbeads_workspace = "{beads}"'
            )
        )
        bead = {
            "id": "central-test",
            "title": "Task",
            "labels": ["project:example"],
            "description": "Implement value",
            "status": "open",
        }
        with (
            mock.patch("afk_run.read_bead", return_value=bead),
            mock.patch.object(creation, "remote_head", return_value=None),
        ):
            result = creation.submit_creation(
                "central-test", self.host, github=self.gh, launcher=lambda *a: None
            )
        d = Path(result["directory"])
        self.assertEqual(jobs.read(d / "bead.json")["description"], "Implement value")
        self.assertEqual(result["job"]["base_branch"], "main")
        self.assertEqual(result["job"]["base"], self.base)

    def completed_fixture(self):
        d = self.submit(fixtures_only=True)
        job = jobs.read(d / "job.json")
        with mock.patch("afk_pr.workspace.subprocess.run", side_effect=self.transport):
            path = workspace.acquire(d, job, "fixtures")
        jobs.write(d / "fixtures.json", {"state": "passed", "publication": "published"})
        (d / "fixtures.stdout.log").write_text("passed")
        (d / "fixtures.stderr.log").write_text("")
        return d, path

    def test_cleanup_retains_active_dirty_failed_and_external_resource(self):
        d, path = self.completed_fixture()
        with mock.patch.object(lifecycle, "quiescent", return_value=False):
            self.assertEqual(lifecycle.cleanup(d)["outcome"], "retained")
        with (d / "lifecycle.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            self.assertEqual(lifecycle.cleanup(d)["outcome"], "retained")
        (path / "untracked").write_text("keep")
        self.assertEqual(lifecycle.cleanup(d)["outcome"], "retained")
        (path / "untracked").unlink()
        jobs.write(d / "fixtures.json", {"state": "failed", "publication": "published"})
        self.assertEqual(lifecycle.cleanup(d)["outcome"], "retained")
        job = jobs.read(d / "job.json")
        job["cleanup_allowed"] = False
        jobs.write(d / "job.json", job)
        self.assertIn("resource", lifecycle.cleanup(d)["reason"])
        self.assertTrue(path.exists())

    def test_cleanup_preserves_evidence_retry_and_refuses_restart(self):
        d, path = self.completed_fixture()
        with mock.patch.object(lifecycle, "quiescent", return_value=True):
            self.assertEqual(lifecycle.cleanup(d, dry_run=True)["outcome"], "eligible")
            self.assertTrue(path.exists())
            self.assertEqual(lifecycle.cleanup(d)["outcome"], "removed")
        self.assertFalse(path.exists())
        self.assertEqual((d / "fixtures.stdout.log").read_text(), "passed")
        with mock.patch.object(jobs, "GitHub", return_value=self.gh):
            jobs.retry_publication(d)
        with self.assertRaisesRegex(ValueError, "cannot restart"):
            jobs.worker(d, "fixtures")
        self.assertEqual(jobs.status_job(d)["cleanup"]["state"], "removed")

    def test_interrupted_cleanup_resumes_and_symlink_root_is_refused(self):
        d, path = self.completed_fixture()
        with (
            mock.patch.object(lifecycle, "quiescent", return_value=True),
            mock.patch.object(
                lifecycle.shutil, "rmtree", side_effect=OSError("interrupted")
            ),
            self.assertRaises(OSError),
        ):
            lifecycle.cleanup(d)
        self.assertEqual(jobs.read(d / "cleanup.json")["state"], "deleting")
        with mock.patch.object(lifecycle, "quiescent", return_value=True):
            self.assertEqual(lifecycle.cleanup(d)["outcome"], "removed")
        self.assertFalse(path.exists())
        d, path = self.completed_fixture()
        parent = path.parent
        relocated = parent.with_name(parent.name + "-elsewhere")
        parent.rename(relocated)
        parent.symlink_to(relocated, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            lifecycle.cleanup(d)
        self.assertTrue((relocated / "fixtures/value").exists())

    def test_resource_identity_and_child_handoff_are_captured(self):
        self.host.write_text(
            self.host.read_text()
            + f'fixture_resource="eqemu"\n[fixture_resources.eqemu]\nworker_home="{self.root}/validation"\nstack_path="{self.root}/stack"\nworkspace_cleanup=false\n'
        )
        d = self.submit(fixtures_only=True)
        job = jobs.read(d / "job.json")
        self.assertEqual(job["fixture_slot"], "resource:eqemu")
        self.assertFalse(job["cleanup_allowed"])
        self.assertEqual(
            job["fixture_resource"]["worker_home"], str(self.root / "validation")
        )
        jobs.write(
            d / "response-progress.json",
            {"candidate": self.head, "changed": True, "push": "pushed"},
        )
        child_id = jobs.queue_fixtures(
            d, job, self.head, self.gh, lambda *a: None, phase="response"
        )
        child = jobs.read(d.parent / child_id / "job.json")
        self.assertEqual(child["fixture_slot"], job["fixture_slot"])
        self.assertEqual(child["policy"], job["policy"])
        self.assertNotEqual(child["id"], job["id"])
        self.assertEqual(child["expected_phases"], ["fixtures"])

    def test_no_change_response_cleanup_and_uncertain_push_retention(self):
        d, path = self.completed_fixture()
        job = jobs.read(d / "job.json")
        job.update(kind="response", expected_phases=["response"])
        jobs.write(d / "job.json", job)
        path.rename(path.with_name("response"))
        jobs.write(
            d / "response.json", {"state": "completed", "publication": "published"}
        )
        jobs.write(
            d / "response-progress.json",
            {"candidate": self.head, "changed": True, "push": "attempted"},
        )
        (d / "response.md").write_text("No repair required")
        (d / "inference").mkdir()
        jobs.write(d / "inference/receipt.json", {"outcome": "succeeded"})
        self.assertIn("uncertain", lifecycle.cleanup(d)["reason"])
        jobs.write(
            d / "response-progress.json",
            {"candidate": self.head, "changed": False, "push": "not_started"},
        )
        with mock.patch.object(lifecycle, "quiescent", return_value=True):
            self.assertEqual(lifecycle.cleanup(d)["outcome"], "removed")

    def test_fixture_auth_and_resource_environment_do_not_store_token(self):
        d = self.submit(fixtures_only=True)
        job = jobs.read(d / "job.json")
        job["validation"]["github_auth"] = True
        job["fixture_resource"] = {
            "worker_home": str(self.root / "worker"),
            "stack_path": str(self.root / "stack"),
        }
        with (
            mock.patch.object(jobs, "checkout", return_value=self.source),
            mock.patch.object(jobs, "git", side_effect=[self.head, ""]),
            mock.patch.object(
                jobs,
                "run_command",
                return_value={"exit_code": 0, "error": None, "timed_out": False},
            ) as run,
        ):
            self.assertEqual(jobs.fixtures(d, job)["state"], "passed")
        command = run.call_args.args[0]
        self.assertIn(
            'set -e; export GITHUB_TOKEN="$(gh auth token)"; exec "$@"', command
        )
        self.assertIn("VALIDATION_WORKER_HOME=" + str(self.root / "worker"), command)
        self.assertIn(
            "VALIDATION_AFK_EVIDENCE_DIR=" + str(d / "fixture-evidence"), command
        )
        self.assertNotIn("GITHUB_TOKEN", (d / "job.json").read_text())

    def test_nested_submodules_are_initialized_in_independent_clone(self):
        sub = self.root / "sub"
        nested = self.root / "nested"
        for path in (nested, sub):
            path.mkdir()
            self.git(path, "init", "-b", "main")
            self.git(path, "config", "user.name", "Fixture")
            self.git(path, "config", "user.email", "fixture@example.invalid")
            (path / "file").write_text("content")
            self.git(path, "add", ".")
            self.git(path, "commit", "-m", "content")
        self.git(
            sub,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            nested.as_uri(),
            "nested",
        )
        self.git(sub, "commit", "-am", "nested module")
        self.git(
            self.source,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            sub.as_uri(),
            "sub",
        )
        self.git(self.source, "commit", "-am", "submodule")
        self.head = self.git(self.source, "rev-parse", "HEAD")
        self.pr["head"]["sha"] = self.head
        d = self.submit(fixtures_only=True)

        def transport(command, **kwargs):
            if command[0] == "git":
                command = ["git", "-c", "protocol.file.allow=always", *command[1:]]
            return self.transport(command, **kwargs)

        with mock.patch("afk_pr.workspace.subprocess.run", side_effect=transport):
            clone = workspace.acquire(d, jobs.read(d / "job.json"), "fixtures")
        self.assertEqual((clone / "sub/nested/file").read_text(), "content")
        self.assertEqual(
            self.git(clone / "sub", "rev-parse", "HEAD"),
            self.git(sub, "rev-parse", "HEAD"),
        )
