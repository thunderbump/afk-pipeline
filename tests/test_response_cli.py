import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
        (self.workspace / "README.md").write_text("reviewed code\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Reviewed state")
        state = self.state()

        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.write_json(
            self.attempt / "input.json",
            {"objective": "Make the reviewed implementation correct."},
        )
        self.review = self.root / "review"
        self.review.mkdir()
        self.write_json(
            self.review / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "attempt_directory": str(self.attempt),
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
                            "worth_addressing": True,
                            "rationale": "The behavior is concrete and reachable.",
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
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["assessment"]["decisions"][0]["worth_addressing"] = False
        self.write_json(self.assessment / "output.json", assessment)
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

    def test_no_action_reobserves_the_workspace_before_sealing(self):
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["assessment"]["decisions"][0]["worth_addressing"] = False
        self.write_json(self.assessment / "output.json", assessment)
        input_path, _result, environment = self.prepare_response("unused")
        result = self.workspace / "response-evidence"

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertTrue(output["repository"]["after"]["dirty"])
        self.assertFalse(output["repository"]["unchanged"])

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
        prompt = marker.read_text()
        self.assertIn("Make the reviewed implementation correct.", prompt)
        self.assertIn("Actionable finding", prompt)
        self.assertNotIn("Dismissed finding", prompt)

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
        self.assertIsNone(output["process"]["exit_code"])
        self.assertIn("missing-agent", output["process"]["error"])
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
            "severity": "medium",
            "title": title,
            "details": "The fixture reports one concrete problem.",
            "locations": [{"path": "README.md", "line": 1}],
        }

    def set_findings_and_decisions(self, findings, worth_addressing):
        review = json.loads((self.review / "output.json").read_text())
        review["review"]["findings"] = findings
        self.write_json(self.review / "output.json", review)
        assessment = json.loads((self.assessment / "output.json").read_text())
        assessment["assessment"]["decisions"] = [
            {
                "finding_index": index,
                "worth_addressing": value,
                "rationale": "Fixture assessment rationale.",
            }
            for index, value in enumerate(worth_addressing)
        ]
        self.write_json(self.assessment / "output.json", assessment)

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
        environment["AFK_RESPOND_AGENT_COMMAND"] = json.dumps(
            command or [sys.executable, str(FIXTURE), scenario]
        )
        return input_path, result, environment

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
