"""Count real validation executions through creation, review and response handoffs."""

import contextlib
import copy
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from afk_orchestrate import driver
from afk_pr import config, decision, jobs, response, validation

URL = "https://github.com/example/repository/pull/1"
REMOTE = "https://github.com/example/repository.git"


class ValidationReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.run = subprocess.run
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        self.counter = self.root / "executions"
        scripts = self.source / "scripts"
        scripts.mkdir()
        (scripts / "validate").write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"with Path({str(self.counter)!r}).open('a') as output: output.write('run\\n')\n"
            "raise SystemExit(1 if Path('value').read_text() == 'bad' else 0)\n"
        )
        (scripts / "validate").chmod(0o755)
        (self.source / "value").write_text("good")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "base")
        self.base = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "switch", "-c", "feature")
        (self.source / "value").write_text("candidate")
        self.git(self.source, "commit", "-am", "candidate")
        self.head = self.git(self.source, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        self.run(
            ["git", "clone", "--bare", str(self.source), str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy = config.fixture_policy(
            {"command": ["./scripts/validate"], "timeout_seconds": 10}
        )
        self.resolved = {
            "layout": "independent-clones-v1",
            "repository": REMOTE,
            "github_repository": "example/repository",
            "workspace_root": str(self.root / "workspaces"),
            "validation": self.policy,
            "policy": {"commit": self.base, "source": "conventional_entrypoint"},
            "review_timeout": 10,
            "cleanup_allowed": True,
        }
        self.gh = mock.Mock()
        self.gh.observe.side_effect = self.context
        self.gh.api.side_effect = lambda *a, **k: self.context()["pull_request"]
        self.gh.fixture_summary.return_value = URL + "#fixtures"
        self.gh.review.return_value = URL + "#review"
        self.gh.comment.return_value = URL + "#response"
        self.launches = []
        self.changed = True
        self.findings = []
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(
            mock.patch.object(
                jobs, "settings", return_value=({"run_root": self.state}, "example", {})
            )
        )
        stack.enter_context(
            mock.patch.object(
                jobs,
                "job_settings",
                side_effect=lambda *a: copy.deepcopy(self.resolved),
            )
        )
        stack.enter_context(
            mock.patch.object(jobs, "checkout", side_effect=self.checkout)
        )
        stack.enter_context(mock.patch.object(jobs, "GitHub", return_value=self.gh))
        stack.enter_context(mock.patch.object(response, "GitHub", return_value=self.gh))
        stack.enter_context(
            mock.patch("afk_inference.runtime.invoke", side_effect=self.invoke)
        )
        self.original_status = jobs.status_job
        stack.enter_context(
            mock.patch.object(jobs, "status_job", side_effect=self.status)
        )
        stack.enter_context(
            mock.patch.object(jobs.subprocess, "run", side_effect=self.transport)
        )

    def git(self, path, *arguments):
        return self.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def transport(self, command, **kwargs):
        if command[0] == "git" and "get-url" not in command:
            command = [
                "git",
                "-c",
                f"url.{self.remote.as_uri()}.insteadOf={REMOTE}",
                *command[1:],
            ]
        return self.run(command, **kwargs)

    def context(self, *args):
        self.head = self.git(self.remote, "rev-parse", "refs/heads/feature")
        return {
            "pull_request": {
                "html_url": URL,
                "state": "open",
                "head": {
                    "sha": self.head,
                    "ref": "feature",
                    "repo": {"full_name": "example/repository"},
                },
                "base": {"sha": self.base, "ref": "main"},
            },
            "comments": [],
            "reviews": [],
            "review_comments": [],
            "commits": [],
            "checks": [],
            "statuses": [],
        }

    def checkout(self, directory, job, phase):
        target = self.root / "workspaces" / job["id"] / phase
        target.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            ["git", "clone", "--quiet", str(self.remote), str(target)],
            check=True,
            capture_output=True,
        )
        self.git(target, "checkout", "--detach", job["head"])
        self.git(target, "remote", "set-url", "origin", REMOTE)
        self.git(target, "config", "user.name", "Fixture")
        self.git(target, "config", "user.email", "fixture@example.invalid")
        return target

    def status(self, directory, *, probe=True):
        result = self.original_status(directory, probe=False)
        for record in result["phases"].values():
            if (
                record["state"] in {"queued", "running"}
                or record.get("publication") == "pending"
            ):
                record["worker_observation"] = "active"
        return result

    def invoke(self, **kwargs):
        if kwargs["purpose"] == "feedback_response":
            if self.changed:
                (kwargs["execution_root"] / "value").write_text("repaired")
            value = "Changed candidate" if self.changed else "No change needed"
        else:
            value = {"summary": "Checked candidate", "findings": self.findings}
        return SimpleNamespace(outcome="succeeded", value=value)

    def launch(self, directory, phase, timeout):
        self.launches.append((directory, phase))

    def submit(self, **kwargs):
        result = jobs.submit(
            URL, self.root / "config", github=self.gh, launcher=self.launch, **kwargs
        )
        return Path(result["directory"])

    def creation(self):
        directory = self.state / "pr-reviews" / ("1" * 16)
        directory.mkdir(parents=True)
        job = {
            **copy.deepcopy(self.resolved),
            "id": directory.name,
            "pr_url": URL,
            "head": self.base,
            "base": self.base,
            "kind": "creation",
            "expected_phases": ["creation"],
            "bead_id": "central-example",
        }
        jobs.write(directory / "job.json", job)
        jobs.write(
            directory / "creation-progress.json",
            {"candidate": self.head, "push": "pushed", "pr_url": URL},
        )
        jobs.write(
            directory / "creation.json",
            {"state": "completed", "publication": "published"},
        )
        child = jobs.queue_fixtures(
            directory, job, self.head, self.gh, self.launch, phase="creation"
        )
        return directory, directory.parent / child

    def complete(self, directory):
        for phase in jobs.read(directory / "job.json")["expected_phases"]:
            jobs.worker(directory, phase)

    def observed(self, directory):
        return decision.observe(URL, self.state, [directory.name], github=self.gh)

    def executions(self):
        return (
            len(self.counter.read_text().splitlines()) if self.counter.exists() else 0
        )

    def test_clean_creation_review_ready_replays_and_repeat_reviews_execute_once(self):
        parent, child = self.creation()
        self.complete(child)
        reviewed = self.submit(action_id="review-one")
        self.complete(reviewed)
        observed = self.observed(reviewed)
        self.assertEqual(observed["decision"]["recommendation"], "continue", observed)
        self.assertEqual(jobs.read(reviewed / "job.json")["fixture_job"], child.name)
        self.assertFalse((reviewed / "fixtures.json").exists())
        repeated = self.submit(action_id="review-two")
        self.complete(repeated)
        self.assertEqual(self.submit(action_id="review-two"), repeated)
        state = {
            "status": "running",
            "stage": "review_wait",
            "active_job": reviewed.name,
            "head": self.head,
            "base": self.base,
            "pr_url": URL,
            "repairs": 0,
            "max_repairs": 2,
            "events": [],
        }
        driver.step(state, lambda *args: observed)
        self.assertEqual(state["status"], "ready_for_merge", state)
        self.assertEqual(self.executions(), 1)
        self.assertEqual(sum(phase == "fixtures" for _, phase in self.launches), 1)
        self.assertEqual(
            self.observed(parent)["decision"]["recommendation"], "continue"
        )

    def test_code_changing_response_validates_new_revision_once_then_review_reuses(
        self,
    ):
        _, original = self.creation()
        self.complete(original)
        repaired = self.submit(respond=True, action_id="respond-one")
        job = jobs.read(repaired / "job.json")
        # Run the real response, including commit/push and child handoff.
        jobs.write(
            repaired / "response.json", {"state": "running", "publication": "pending"}
        )
        result = response.respond(repaired, job, github=self.gh, launcher=self.launch)
        jobs.write(repaired / "response.json", {**result, "publication": "published"})
        progress = jobs.read(repaired / "response-progress.json")
        candidate = repaired.parent / progress["fixture_job"]
        self.assertNotEqual(candidate, original)
        self.complete(candidate)
        reviewed = self.submit(action_id="review-repair")
        self.complete(reviewed)
        self.assertEqual(
            self.observed(reviewed)["decision"]["recommendation"], "continue"
        )
        self.assertEqual(
            self.observed(repaired)["decision"]["recommendation"], "continue"
        )
        self.assertEqual(self.executions(), 2)
        self.assertEqual(sum(phase == "fixtures" for _, phase in self.launches), 2)

    def test_no_change_response_and_review_add_no_validation(self):
        _, child = self.creation()
        self.complete(child)
        self.changed = False
        replied = self.submit(respond=True)
        self.complete(replied)
        self.assertFalse(jobs.read(replied / "response-progress.json")["changed"])
        reviewed = self.submit()
        self.complete(reviewed)
        self.assertEqual(self.executions(), 1)
        self.assertEqual(
            self.observed(reviewed)["decision"]["recommendation"], "continue"
        )

    def test_standalone_review_without_evidence_validates_and_reports_findings(self):
        self.findings = [{"message": "A useful concern"}]
        reviewed = self.submit()
        self.complete(reviewed)
        self.assertEqual(self.executions(), 1)
        self.assertEqual(
            jobs.read(reviewed / "review.json")["result"]["findings"], self.findings
        )

    def test_active_matching_fixture_is_attached_and_readiness_waits_for_it(self):
        _, child = self.creation()
        reviewed = self.submit()
        self.complete(reviewed)
        self.assertEqual(jobs.read(reviewed / "job.json")["fixture_job"], child.name)
        self.assertEqual(self.observed(reviewed)["decision"]["recommendation"], "wait")
        self.assertEqual(sum(phase == "fixtures" for _, phase in self.launches), 1)
        self.complete(child)
        self.assertEqual(
            self.observed(reviewed)["decision"]["recommendation"], "continue"
        )

    def test_published_candidate_failure_is_reused_without_false_green_or_rebuild(self):
        (self.source / "value").write_text("bad")
        self.git(self.source, "commit", "-am", "broken candidate")
        self.git(self.source, "push", str(self.remote), "feature")
        self.context()
        _, child = self.creation()
        self.complete(child)
        reviewed = self.submit()
        self.complete(reviewed)
        observed = self.observed(reviewed)
        self.assertEqual(observed["decision"]["recommendation"], "pause")
        self.assertTrue(driver.repairable_validation(observed), observed)
        self.assertEqual(self.executions(), 1)

    def test_supervisor_failure_response_revision_review_reaches_ready_with_two_executions(
        self,
    ):
        (self.source / "value").write_text("bad")
        self.git(self.source, "commit", "-am", "broken candidate")
        self.git(self.source, "push", str(self.remote), "feature")
        self.context()
        parent, child = self.creation()
        self.complete(child)
        real_response = response.respond
        with mock.patch.object(
            response,
            "respond",
            side_effect=lambda directory, job: real_response(
                directory, job, github=self.gh, launcher=self.launch
            ),
        ):

            def commands(*args):
                if args[0] == "pr":
                    return jobs.status_job(parent)
                if args[0] == "job":
                    return jobs.status_job(self.state / "pr-reviews" / args[1])
                if args[0] == "status":
                    return decision.observe(URL, self.state, [args[3]], github=self.gh)
                if args[0] in {"respond", "review"}:
                    result = jobs.submit(
                        URL,
                        self.root / "config",
                        respond=args[0] == "respond",
                        action_id=args[3],
                        expected_head=args[5],
                        github=self.gh,
                        launcher=self.launch,
                    )
                    directory = Path(result["directory"])
                    self.complete(directory)
                    if args[0] == "respond":
                        progress = jobs.read(directory / "response-progress.json")
                        self.complete(directory.parent / progress["fixture_job"])
                    return result
                raise AssertionError(args)

            path, _ = driver.create(
                self.root / "orchestrations",
                "central-example",
                self.root / "config",
                max_repairs=2,
            )
            for _ in range(12):
                state = driver.advance(path, commands)
                if state["status"] != "running":
                    break
        self.assertEqual(state["status"], "ready_for_merge", state)
        self.assertEqual(state["repairs"], 1)
        self.assertEqual(self.executions(), 2)
        self.assertEqual(sum(phase == "fixtures" for _, phase in self.launches), 2)

    def test_stale_unknown_tampered_unpublished_and_interrupted_evidence_revalidate(
        self,
    ):
        _, child = self.creation()
        self.complete(child)
        saved_job = jobs.read(child / "job.json")
        saved_result = jobs.read(child / "fixtures.json")
        mutations = (
            ("wrong head", "job", {"head": "f" * 40}),
            ("wrong base", "job", {"base": "f" * 40}),
            (
                "wrong repository",
                "job",
                {"pr_url": URL.replace("example/repository", "other/repository")},
            ),
            ("unknown identity", "job", {"fixture_contract": None}),
            ("missing publication", "result", {"publication": "failed"}),
            ("tampered result", "result", {"url": URL + "#changed"}),
            ("timed out", "result", {"state": "timed_out"}),
            ("interrupted", "result", {"state": "interrupted"}),
            ("candidate mutated", "result", {"candidate_unchanged": False}),
        )
        for name, kind, fields in mutations:
            with self.subTest(name=name):
                jobs.write(
                    child / "job.json",
                    {**saved_job, **(fields if kind == "job" else {})},
                )
                jobs.write(
                    child / "fixtures.json",
                    {**saved_result, **(fields if kind == "result" else {})},
                )
                reviewed = self.submit()
                self.assertNotIn("fixture_job", jobs.read(reviewed / "job.json"))
                # Remove the new reservation from consideration; it has not executed.
                jobs.write(
                    reviewed / "fixtures.json",
                    {"state": "interrupted", "publication": "pending"},
                )
        jobs.write(child / "job.json", saved_job)
        jobs.write(child / "fixtures.json", saved_result)
        (child / "fixtures.stdout.log").write_text("tampered logs")
        reviewed = self.submit()
        self.assertNotIn("fixture_job", jobs.read(reviewed / "job.json"))

    def test_external_adapter_identity_change_and_unknown_identity_force_new_validation(
        self,
    ):
        identity = self.root / "identity"
        identity.write_text("release-profile-inputs-v1")
        self.policy["identity_command"] = [
            sys.executable,
            "-c",
            f"import json;from pathlib import Path;print(json.dumps({{'schema_version':1,'identity':Path({str(identity)!r}).read_text()}}))",
        ]
        _, child = self.creation()
        self.complete(child)
        self.assertIn("fixture_job", jobs.read(self.submit() / "job.json"))
        identity.write_text("release-profile-inputs-v2")
        fresh = self.submit()
        self.assertNotIn("fixture_job", jobs.read(fresh / "job.json"))
        self.complete(fresh)
        identity.unlink()
        unknown = self.submit()
        self.assertNotIn("fixture_job", jobs.read(unknown / "job.json"))
        self.assertIsNone(jobs.read(unknown / "job.json")["fixture_contract"])

    def test_changed_policy_and_external_inputs_invalidate_matching_revision(self):
        _, child = self.creation()
        self.complete(child)
        self.policy["timeout_seconds"] = 11
        fresh = self.submit()
        self.assertNotIn("fixture_job", jobs.read(fresh / "job.json"))
        self.complete(fresh)
        self.policy["command"] = ["./scripts/validate", "another-profile"]
        changed_profile = self.submit()
        self.assertNotIn("fixture_job", jobs.read(changed_profile / "job.json"))
        self.complete(changed_profile)
        self.assertEqual(self.executions(), 3)

    def test_fixtures_only_reuses_original_owner_without_a_forged_phase(self):
        _, child = self.creation()
        self.complete(child)
        selected = self.submit(fixtures_only=True)
        job = jobs.read(selected / "job.json")
        self.assertEqual(job["fixture_job"], child.name)
        self.assertEqual(job["expected_phases"], [])
        self.assertFalse((selected / "fixtures.json").exists())
        self.assertEqual(
            self.observed(selected)["decision"]["recommendation"], "continue"
        )
        self.assertEqual(self.executions(), 1)

    def test_unknown_contract_fresh_execution_is_valid_but_never_reused(self):
        self.policy["identity_command"] = [sys.executable, "-c", "raise SystemExit(2)"]
        first = self.submit()
        self.complete(first)
        self.assertIsNone(jobs.read(first / "job.json")["fixture_contract"])
        self.assertEqual(self.observed(first)["decision"]["recommendation"], "continue")
        second = self.submit()
        self.assertNotIn("fixture_job", jobs.read(second / "job.json"))
        self.complete(second)
        self.assertEqual(self.executions(), 2)

    def test_removing_contract_cannot_bypass_new_execution_integrity(self):
        (self.source / "value").write_text("bad")
        self.git(self.source, "commit", "-am", "broken candidate")
        self.git(self.source, "push", str(self.remote), "feature")
        self.context()
        _, child = self.creation()
        self.complete(child)
        saved_job = jobs.read(child / "job.json")
        saved_result = jobs.read(child / "fixtures.json")
        jobs.write(child / "job.json", {**saved_job, "fixture_contract": None})
        (child / "fixtures.stdout.log").write_text("tampered execution")
        changed = copy.deepcopy(saved_result)
        changed["state"] = "passed"
        changed["process"]["exit_code"] = 0
        jobs.write(child / "fixtures.json", changed)
        observed = self.observed(child)
        self.assertEqual(observed["decision"]["recommendation"], "pause", observed)
        self.assertIn(
            "validation_contract_or_evidence_changed",
            {reason["code"] for reason in observed["decision"]["reasons"]},
        )

    def test_publication_failure_recovers_without_execution_or_seal_changes(self):
        parent, child = self.creation()
        self.gh.fixture_summary.side_effect = RuntimeError(
            "temporary publication failure"
        )
        self.complete(child)
        self.assertEqual(jobs.read(child / "fixtures.json")["publication"], "failed")
        original_seal = (child / "fixtures-seal.json").read_bytes()
        observed = self.observed(parent)
        self.assertEqual(
            observed["decision"]["recommendation"], "retry_publication", observed
        )
        self.assertEqual(
            observed["decision"]["publication_retry_job_ids"], [child.name]
        )
        self.gh.fixture_summary.side_effect = None
        jobs.retry_publication(child)
        self.assertEqual(
            self.observed(parent)["decision"]["recommendation"], "continue"
        )
        self.assertEqual((child / "fixtures-seal.json").read_bytes(), original_seal)
        reviewed = self.submit()
        self.complete(reviewed)
        self.assertEqual(self.executions(), 1)

    def test_republication_refuses_result_or_log_tampering_instead_of_resealing(self):
        (self.source / "value").write_text("bad")
        self.git(self.source, "commit", "-am", "broken candidate")
        self.git(self.source, "push", str(self.remote), "feature")
        self.context()
        _, child = self.creation()
        self.complete(child)
        original = jobs.read(child / "fixtures.json")
        original_seal = (child / "fixtures-seal.json").read_bytes()
        for alteration in ("result", "log"):
            with self.subTest(alteration=alteration):
                changed = copy.deepcopy(original)
                if alteration == "result":
                    changed["state"] = "passed"
                    changed["process"]["exit_code"] = 0
                else:
                    (child / "fixtures.stdout.log").write_text(
                        "fabricated successful output"
                    )
                jobs.write(child / "fixtures.json", changed)
                status_calls = self.gh.fixture_status.call_count
                with self.assertRaisesRegex(ValueError, "execution evidence changed"):
                    jobs.retry_publication(child)
                self.assertEqual(self.gh.fixture_status.call_count, status_calls)
                self.assertEqual(
                    (child / "fixtures-seal.json").read_bytes(), original_seal
                )
                self.assertEqual(
                    self.observed(child)["decision"]["recommendation"], "pause"
                )
        self.assertEqual(self.executions(), 1)

    def test_identity_drift_during_execution_cannot_pass_or_be_reused(self):
        identity = self.root / "identity"
        identity.write_text("release-v1")
        self.policy["identity_command"] = [
            sys.executable,
            "-c",
            f"import json;from pathlib import Path;print(json.dumps({{'schema_version':1,'identity':Path({str(identity)!r}).read_text()}}))",
        ]
        reviewed = self.submit()
        real_run = jobs.run_command

        def run(*args, **kwargs):
            result = real_run(*args, **kwargs)
            identity.write_text("release-v2")
            return result

        with mock.patch.object(jobs, "run_command", side_effect=run):
            self.complete(reviewed)
        record = jobs.read(reviewed / "fixtures.json")
        self.assertEqual(record["state"], "failed")
        self.assertFalse(record["contract_unchanged"])
        self.assertFalse(driver.repairable_validation(self.observed(reviewed)))
        self.assertNotIn("fixture_job", jobs.read(self.submit() / "job.json"))

    def test_simultaneous_review_handoffs_launch_one_fixture(self):
        entered, release = threading.Event(), threading.Event()

        def launch(directory, phase, timeout):
            self.launch(directory, phase, timeout)
            if phase == "fixtures":
                entered.set()
                self.assertTrue(release.wait(5))

        results, errors = [], []

        def first():
            try:
                results.append(
                    jobs.submit(
                        URL,
                        self.root / "config",
                        github=self.gh,
                        launcher=launch,
                        action_id="first",
                    )
                )
            except (OSError, ValueError, RuntimeError, AssertionError) as error:
                errors.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(ValueError, "busy"):
                self.submit(action_id="second")
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(errors, errors)
        second = self.submit(action_id="second")
        self.assertEqual(
            jobs.read(second / "job.json")["fixture_job"], results[0]["job"]["id"]
        )
        self.assertEqual(sum(phase == "fixtures" for _, phase in self.launches), 1)


class IdentityProbeTests(unittest.TestCase):
    def test_timeout_kills_the_probe_without_waiting_for_its_sleep(self):
        with mock.patch.object(validation.time, "monotonic", side_effect=[0, 11]):
            self.assertIsNone(
                validation.probe_identity(
                    [sys.executable, "-c", "import time;time.sleep(60)"]
                )
            )

    def test_protocol_unknown_oversized_invalid_and_failed_outputs(self):
        for code in (
            "print('x' * 1000000)",
            "print('{}')",
            "print('bad json')",
            'print(\'{"schema_version":true,"identity":"release"}\')',
            'print(\'{"schema_version":1,"identity":"release","other":1}\')',
            "raise SystemExit(1)",
            'print(\'{"schema_version":1,"identity":""}\')',
            "import json;print(json.dumps({'schema_version':1,'identity':'x'*257}))",
        ):
            with self.subTest(code=code):
                job = {
                    "pr_url": URL,
                    "head": "a" * 40,
                    "base": "b" * 40,
                    "validation": {
                        "command": ["/external/validate"],
                        "identity_command": [sys.executable, "-c", code],
                    },
                }
                self.assertIsNone(validation.contract(job))
        job["validation"]["identity_command"] = [
            sys.executable,
            "-c",
            'print(\'{"schema_version":1,"identity":"sealed-v1"}\')',
        ]
        self.assertIsNotNone(validation.contract(job))

    def test_external_unknown_and_invalid_config_are_safe(self):
        for command in (["/external/validate"], ["validate"], ["./../validate"]):
            self.assertIsNone(
                validation.contract(
                    {
                        "pr_url": URL,
                        "head": "a" * 40,
                        "base": "b" * 40,
                        "validation": {"command": command},
                    }
                )
            )
        for probe in ([], "identity", [""], [1]):
            with self.assertRaisesRegex(ValueError, "identity_command"):
                config.fixture_policy({"identity_command": probe})
