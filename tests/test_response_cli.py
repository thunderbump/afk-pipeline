import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from afk_review.contract import REVIEW_AUDIT

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_response_agent.py"


class PublicResponseCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_respond", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "usage: python3 -m afk_respond RESPONSE_JSON RESULT_DIRECTORY",
            help_result.stdout,
        )
        self.assertIn("feedback-response JSON", help_result.stdout)
        self.assertEqual(help_result.stderr, "")

        invalid = subprocess.run(
            [sys.executable, "-m", "afk_respond", "response.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage: python3 -m afk_respond", invalid.stderr)


class ResponseCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("before review\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Before implementation")
        before = self.state()
        (self.workspace / "README.md").write_text("reviewed code\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Reviewed state")
        state = self.state()

        self.change = self.root / "committed-change"
        self.change.mkdir()
        self.write_json(
            self.change / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "objective": "Make the reviewed implementation correct.",
                    "workspace": str(self.workspace),
                    "repository": {"before": before, "after": state},
                    "source": {
                        "kind": "attempt",
                        "directory": str(self.root / "attempt"),
                    },
                },
            },
        )
        self.review = self.root / "review"
        self.review.mkdir()
        self.write_json(
            self.review / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "change_directory": str(self.change),
                "validation_directory": str(self.root / "validation"),
                "timeout_seconds": 5,
            },
        )
        self.write_json(
            self.review / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "review": {
                    "summary": "One finding reported.",
                    "findings": [self.finding("Actionable finding")],
                    "audit": REVIEW_AUDIT,
                },
                "repository": {"before": state, "after": state, "unchanged": True},
                "artifacts": {
                    "diff": "diff.patch",
                    "events": "events.jsonl",
                    "stderr": "stderr.log",
                },
            },
        )
        (self.review / "diff.patch").write_text("fixture diff\n")

        self.assessment = self.root / "assessment"
        self.assessment.mkdir()
        self.write_json(
            self.assessment / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "review_directory": str(self.review),
                "timeout_seconds": 5,
            },
        )
        self.write_json(
            self.assessment / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {
                    "summary": "The finding should be addressed.",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "defect_decision": "confirmed",
                            "rationale": "The behavior is concrete and reachable.",
                            "scope": {
                                "kind": "current",
                                "rationale": "The current objective owns this behavior.",
                            },
                        }
                    ],
                },
                "repository": {"before": state, "after": state, "unchanged": True},
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            },
        )

    def test_completed_response_requires_a_clean_commit_and_seals_artifacts(self):
        before_head = self.git("rev-parse", "HEAD")

        result, completed = self.run_response("commit")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        receipt = json.loads((result / "inference/receipt.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "WRITE")
        self.assertEqual(json.loads(receipt["terminal_response"]), output["response"])
        self.assertEqual(output["agent"], {"status": "completed"})
        self.assertEqual(
            output["response"]["finding_responses"],
            [
                {
                    "finding_index": 0,
                    "response": "Updated the implementation and committed the change.",
                }
            ],
        )
        self.assertEqual(output["repository"]["before"]["head"], before_head)
        self.assertNotEqual(output["repository"]["after"]["head"], before_head)
        self.assertFalse(output["repository"]["after"]["dirty"])
        self.assertEqual(
            output["repository"]["commits_between_heads"],
            [output["repository"]["after"]["head"]],
        )
        self.assertTrue((result / "events.jsonl").is_file())
        self.assertEqual((result / "stderr.log").read_text(), "")
        self.assertFalse((result / "output.json.tmp").exists())

    def test_no_action_completes_without_launching_an_agent(self):
        self.make_no_action()
        before = self.state()

        result, completed = self.run_response(
            "unused", command=[str(self.root / "missing-agent")]
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertIsNone(output["process"])
        self.assertIsNone(output["agent"])
        self.assertEqual(output["response"]["finding_responses"], [])
        self.assertEqual(output["repository"]["before"], before)
        self.assertEqual(output["repository"]["after"], before)
        self.assertEqual((result / "events.jsonl").read_text(), "")
        self.assertEqual((result / "stderr.log").read_text(), "")
        self.assertFalse((result / "inference").exists())

    def test_no_action_reobserves_the_workspace_before_sealing(self):
        self.make_no_action()
        input_path, _result, environment = self.prepare_response("unused")
        result = self.workspace / "response-evidence"

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertTrue(output["repository"]["after"]["dirty"])
        self.assertFalse(output["repository"]["unchanged"])

    def test_no_action_records_a_concurrent_commit(self):
        self.make_no_action()
        input_path, result, environment = self.prepare_response("unused")
        self.wrap_git(environment, "commit")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        repository = output["repository"]
        self.assertEqual(output["outcome"], "failed")
        self.assertNotEqual(repository["after"]["head"], repository["before"]["head"])
        self.assertEqual(
            repository["commits_between_heads"], [repository["after"]["head"]]
        )
        self.assertTrue(repository["descends_from_before"])

    def test_no_action_records_unknown_facts_when_final_observation_fails(self):
        self.make_no_action()
        input_path, result, environment = self.prepare_response("unused")
        self.wrap_git(environment, "fail")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        repository = output["repository"]
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(repository["after"])
        self.assertIsNone(repository["commits_between_heads"])
        self.assertIsNone(repository["descends_from_before"])
        self.assertIn("observation_error", repository)

    def test_only_actionable_findings_are_required_and_given_to_the_agent(self):
        self.set_findings_and_decisions(
            [self.finding("Actionable finding"), self.finding("Dismissed finding")],
            [True, False],
        )
        marker = self.root / "prompt.txt"

        result, completed = self.run_response(
            "capture-prompt",
            command=[sys.executable, str(FIXTURE), "capture-prompt", str(marker)],
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(
            [item["finding_index"] for item in output["response"]["finding_responses"]],
            [0],
        )
        prompt = json.loads((result / "inference/prompt.json").read_text())
        task = prompt["untrusted_task_data"]
        self.assertEqual(task["objective"], "Make the reviewed implementation correct.")
        self.assertEqual(
            [item["finding"]["title"] for item in task["actionable_findings"]],
            ["Actionable finding"],
        )

    def test_validation_repair_identifies_failure_artifacts_and_is_not_review_feedback(
        self,
    ):
        attempt = self.root / "attempt"
        attempt.mkdir()
        self.write_json(
            attempt / "input.json",
            {
                "schema_version": 1,
                "objective": "Make validation pass.",
                "workspace": str(self.workspace),
                "command": ["unused"],
                "timeout_seconds": 5,
            },
        )
        state = self.state()
        prior = {
            **state,
            "head": self.git("rev-parse", "HEAD^"),
        }
        self.write_json(
            attempt / "output.json",
            {
                "schema_version": 1,
                "outcome": "succeeded",
                "repository": {
                    "before": prior,
                    "after": state,
                    "commits_between_heads": [state["head"]],
                },
            },
        )
        validation = self.root / "failed-validation"
        validation.mkdir()
        self.write_json(
            validation / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "command": ["./scripts/validate"],
                "timeout_seconds": 5,
            },
        )
        self.write_json(
            validation / "output.json",
            {
                "schema_version": 1,
                "outcome": "failed",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1.0,
                "process": {"exit_code": 7, "signal": None},
                "repository": {
                    "before": state,
                    "after": state,
                    "head_changed": False,
                },
                "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
            },
        )
        (validation / "stdout.log").write_text("failing test output\n")
        (validation / "stderr.log").write_text("failure detail\n")
        response_input = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "validation_directory": str(validation),
            "source": {"kind": "attempt", "directory": str(attempt)},
            "objective": "Make validation pass.",
            "timeout_seconds": 5,
        }
        input_path = self.root / "validation-response.json"
        self.write_json(input_path, response_input)
        result = self.root / "validation-response"
        marker_path = self.root / "validation-prompt.txt"
        environment = os.environ.copy()
        self.install_pi_fixture(
            environment,
            [
                sys.executable,
                str(FIXTURE),
                "capture-validation-prompt",
                str(marker_path),
            ],
        )

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["response"]["finding_responses"], [])
        prompt = json.loads((result / "inference/prompt.json").read_text())
        failure = prompt["untrusted_task_data"]["failed_validation"]
        self.assertEqual(failure["directory"], str(validation))
        self.assertEqual(failure["stdout"], "failing test output\n")
        self.assertEqual(failure["stderr"], "failure detail\n")
        self.assertIn(
            "not an accepted Review finding", prompt["trusted_task_instructions"]
        )

    def test_validation_repair_refuses_launch_error_and_repository_drift_evidence(self):
        from afk_validate.evidence import validate_repairable_failure

        validation = self.root / "refused-validation"
        validation.mkdir()
        state = self.state()
        self.write_json(
            validation / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "command": ["missing"],
                "timeout_seconds": 5,
            },
        )
        (validation / "stdout.log").touch()
        (validation / "stderr.log").touch()
        base = {
            "schema_version": 1,
            "outcome": "failed",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "duration_seconds": 1.0,
            "repository": {"before": state, "after": state, "head_changed": False},
            "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
        }
        for process in (
            {"exit_code": None, "signal": None, "error": "launch failed"},
            {"exit_code": None, "signal": "SIGTERM"},
        ):
            with self.subTest(process=process):
                self.write_json(
                    validation / "output.json", {**base, "process": process}
                )
                with self.assertRaises(ValueError):
                    validate_repairable_failure(validation, self.workspace, state)
        self.write_json(
            validation / "output.json",
            {**base, "process": {"exit_code": 7, "signal": None}},
        )
        drifted = {**state, "head": "different"}
        with self.assertRaisesRegex(ValueError, "drifted"):
            validate_repairable_failure(validation, self.workspace, drifted)

        (validation / "stderr.log").unlink()
        with self.assertRaisesRegex(ValueError, "logs are unavailable"):
            validate_repairable_failure(validation, self.workspace, state)
        (validation / "stderr.log").touch()
        self.write_json(
            validation / "output.json",
            {
                **base,
                "outcome": "timed_out",
                "process": {"exit_code": None, "signal": "SIGTERM"},
            },
        )
        with self.assertRaisesRegex(ValueError, "invalid failed Validation output"):
            validate_repairable_failure(validation, self.workspace, state)

    def test_invalid_input_existing_result_and_stale_workspace_are_refused(self):
        input_path, result, environment = self.prepare_response("commit")
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 0
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

        input_path, result, environment = self.prepare_response(
            "commit", result_name="existing"
        )
        result.mkdir()
        sentinel = result / "keep.txt"
        sentinel.write_text("caller data\n")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual([sentinel], list(result.iterdir()))

        (self.workspace / "newer.txt").write_text("stale assessment\n")
        input_path, result, environment = self.prepare_response(
            "commit", result_name="stale"
        )

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("workspace must match", completed.stderr)

    def test_malformed_or_noncompleted_evidence_is_refused_before_result_creation(self):
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["outcome"] = "failed"
        self.write_json(self.assessment / "output.json", assessment)

        result, completed = self.run_response("commit")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("requires a completed Finding Assessment", completed.stderr)

        assessment["outcome"] = "completed"
        assessment["assessment"]["decisions"] = []
        self.write_json(self.assessment / "output.json", assessment)

        result, completed = self.run_response("commit", result_name="malformed")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("each Review finding", completed.stderr)

    def test_invalid_agent_or_response_protocol_is_sealed(self):
        for scenario, expected_error in (
            ("invalid-events", None),
            ("invalid-json", "Expecting value"),
            ("missing-response", "each actionable finding"),
            ("wrong-index", "each actionable finding"),
        ):
            with self.subTest(scenario=scenario):
                result, completed = self.run_response(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["response"])
                if expected_error:
                    self.assertIn(expected_error, output["response_error"])

    def test_agent_launch_failure_dirty_and_unchanged_results_are_sealed(self):
        result, completed = self.run_response(
            "unused",
            result_name="launch",
            command=[str(self.root / "missing-agent")],
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertNotEqual(output["process"]["exit_code"], 0)
        self.assertIsNone(output["agent"])

        for scenario in ("dirty", "unchanged"):
            with self.subTest(scenario=scenario):
                result, completed = self.run_response(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                if scenario == "dirty":
                    self.git("restore", "README.md")

    def test_post_response_git_damage_is_sealed(self):
        result, completed = self.run_response("damage-git")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIn("observation_error", output["repository"])

    def test_timeout_and_interrupt_terminate_the_agent_process_group(self):
        marker = self.root / "timed-out-descendant.pid"
        input_path, result, environment = self.prepare_response(
            "hang", command=[sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 1
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"], "timed_out"
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

        marker = self.root / "interrupted-descendant.pid"
        input_path, result, environment = self.prepare_response(
            "hang",
            result_name="interrupted",
            command=[sys.executable, str(FIXTURE), "hang", str(marker)],
        )
        responder = subprocess.Popen(
            [sys.executable, "-m", "afk_respond", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(marker.is_file())
        responder.send_signal(signal.SIGINT)
        _stdout, stderr = responder.communicate(timeout=5)

        self.assertEqual(responder.returncode, 1, stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"], "interrupted"
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_closed_progress_stdout_does_not_prevent_sealing(self):
        input_path, result, environment = self.prepare_response("delayed-commit")
        responder = subprocess.Popen(
            [sys.executable, "-m", "afk_respond", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert responder.stdout is not None
        for line in responder.stdout:
            if "starting feedback-response agent" in line:
                break
        else:
            self.fail("feedback-response agent did not start")
        responder.stdout.close()
        assert responder.stderr is not None
        stderr = responder.stderr.read()
        responder.stderr.close()

        self.assertEqual(responder.wait(timeout=5), 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"], "completed"
        )

    def finding(self, title):
        return {
            "lens": "behavior",
            "title": title,
            "details": "The fixture reports one concrete problem.",
            "locations": [{"path": "README.md", "line": 1}],
            "scope_claim": {
                "kind": "current",
                "rationale": "The current objective owns this behavior.",
            },
        }

    def set_findings_and_decisions(self, findings, worth_addressing):
        review = json.loads((self.review / "output.json").read_text())
        review["review"]["findings"] = findings
        self.write_json(self.review / "output.json", review)
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["assessment"]["decisions"] = [
            {
                "finding_index": index,
                "defect_decision": "confirmed" if value else "rejected",
                "rationale": "Fixture assessment rationale.",
                "scope": {
                    "kind": "current",
                    "rationale": "The current objective owns this behavior.",
                },
            }
            for index, value in enumerate(worth_addressing)
        ]
        self.write_json(self.assessment / "output.json", assessment)

    def make_no_action(self):
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["assessment"]["decisions"][0]["defect_decision"] = "rejected"
        self.write_json(self.assessment / "output.json", assessment)

    def wrap_git(self, environment, mode):
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        wrapper = bin_directory / "git"
        wrapper.write_text(
            """#!/bin/sh
count=$(($(cat "$AFK_TEST_GIT_COUNT" 2>/dev/null || echo 0) + 1))
printf '%s' "$count" > "$AFK_TEST_GIT_COUNT"
if [ "$count" -eq 6 ]; then
  if [ "$AFK_TEST_GIT_MODE" = commit ]; then
    printf 'concurrent change\\n' > "$AFK_TEST_WORKSPACE/concurrent.txt"
    /usr/bin/git -C "$AFK_TEST_WORKSPACE" add concurrent.txt
    /usr/bin/git -C "$AFK_TEST_WORKSPACE" commit --quiet -m 'Concurrent change'
  else
    exit 1
  fi
fi
exec /usr/bin/git "$@"
"""
        )
        wrapper.chmod(0o755)
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        environment["AFK_TEST_GIT_COUNT"] = str(self.root / "git-count")
        environment["AFK_TEST_GIT_MODE"] = mode
        environment["AFK_TEST_WORKSPACE"] = str(self.workspace)

    def run_response(self, scenario, result_name="response", command=None):
        input_path, result, environment = self.prepare_response(
            scenario, result_name, command
        )
        return result, self.invoke(input_path, result, environment)

    def prepare_response(self, scenario, result_name="response", command=None):
        response_input = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "assessment_directory": str(self.assessment),
            "timeout_seconds": 5,
        }
        input_path = self.root / "response.json"
        self.write_json(input_path, response_input)
        result = self.root / result_name
        environment = os.environ.copy()
        self.install_pi_fixture(
            environment, command or [sys.executable, str(FIXTURE), scenario]
        )
        return input_path, result, environment

    def install_pi_fixture(self, environment, command):
        bin_directory = self.root / "inference-bin"
        bin_directory.mkdir(exist_ok=True)
        rendered = " ".join(shlex.quote(item) for item in command)
        pi = bin_directory / "pi"
        pi.write_text(f'#!/bin/sh\nexec {rendered} "$@"\n')
        pi.chmod(0o755)
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

    def invoke(self, input_path, result, environment):
        return subprocess.run(
            [sys.executable, "-m", "afk_respond", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def state(self):
        status = self.git("status", "--porcelain").splitlines()
        return {
            "head": self.git("rev-parse", "HEAD"),
            "branch": "main",
            "dirty": bool(status),
            "status": status,
        }

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.workspace,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def write_json(self, path, value):
        path.write_text(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
