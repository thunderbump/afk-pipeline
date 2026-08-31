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

from afk_related_work import build_snapshot, reference
from afk_review.contract import REVIEW_AUDIT
from tests.inference_cli_fixture import install_pi

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_assessment_agent.py"


class PublicAssessmentCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_assess", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "usage: python3 -m afk_assess ASSESSMENT_JSON RESULT_DIRECTORY",
            help_result.stdout,
        )
        self.assertIn("finding-assessment JSON", help_result.stdout)
        self.assertEqual(help_result.stderr, "")

        invalid = subprocess.run(
            [sys.executable, "-m", "afk_assess", "assessment.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage: python3 -m afk_assess", invalid.stderr)


class AssessmentCliTest(unittest.TestCase):
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
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "Fixture finding",
                            "details": "The fixture reports one concrete problem.",
                            "locations": [{"path": "README.md", "line": 1}],
                        }
                    ],
                    "audit": REVIEW_AUDIT,
                },
                "repository": {
                    "before": state,
                    "after": state,
                    "unchanged": True,
                },
                "artifacts": {
                    "diff": "diff.patch",
                    "events": "events.jsonl",
                    "stderr": "stderr.log",
                },
            },
        )
        (self.review / "diff.patch").write_text("fixture diff\n")

    def test_completed_assessment_seals_one_decision_per_finding(self):
        result, completed = self.run_assessment("address")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(
            output["assessment"],
            {
                "summary": "The finding should be addressed.",
                "decisions": [
                    {
                        "finding_index": 0,
                        "worth_addressing": True,
                        "rationale": (
                            "The behavior is reachable and violates the objective."
                        ),
                    }
                ],
            },
        )
        self.assertEqual(output["repository"]["before"], self.state())
        self.assertTrue(output["repository"]["unchanged"])
        self.assertEqual(output["artifacts"]["events"], "events.jsonl")
        self.assertTrue((result / "events.jsonl").is_file())
        self.assertEqual((result / "stderr.log").read_text(), "")
        receipt = json.loads((result / "inference/receipt.json").read_text())
        invocation = json.loads((result / "inference/invocation.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "READ_ONLY")
        self.assertEqual(invocation["execution_root"], str(self.workspace))
        self.assertFalse((result / "output.json.tmp").exists())

    def test_completed_shapes_cover_no_findings_dismiss_and_mixed(self):
        cases = (
            ("no-findings", [], []),
            ("dismiss", [self.finding("First")], [False]),
            (
                "mixed",
                [self.finding("First"), self.finding("Second")],
                [True, False],
            ),
        )
        for scenario, findings, expected in cases:
            with self.subTest(scenario=scenario):
                self.set_findings(findings)
                result, completed = self.run_assessment(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "completed")
                self.assertEqual(
                    [
                        decision["worth_addressing"]
                        for decision in output["assessment"]["decisions"]
                    ],
                    expected,
                )

    def test_invalid_decision_coverage_and_schema_are_sealed_failures(self):
        self.set_findings([self.finding("First"), self.finding("Second")])
        for scenario, message in (
            ("missing-decision", "each Review finding must have one decision"),
            ("duplicate-decision", "each Review finding must have one decision"),
            ("invalid-decision", "worth_addressing must be a boolean"),
        ):
            with self.subTest(scenario=scenario):
                result, completed = self.run_assessment(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["assessment"])
                self.assertIn(message, output["assessment_error"])

    def test_invalid_input_and_existing_result_are_refused_without_changes(self):
        input_path, result, environment = self.prepare_assessment("address")
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 0
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

        input_path, result, environment = self.prepare_assessment("address")
        result.mkdir()
        sentinel = result / "keep.txt"
        sentinel.write_text("caller data\n")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual([sentinel], list(result.iterdir()))

    def test_review_must_be_completed_and_match_the_clean_workspace(self):
        output = json.loads((self.review / "output.json").read_text())
        output["outcome"] = "failed"
        self.write_json(self.review / "output.json", output)

        result, completed = self.run_assessment("address")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("requires a completed Review", completed.stderr)

        output["outcome"] = "completed"
        self.write_json(self.review / "output.json", output)
        (self.workspace / "unreviewed.txt").write_text("different state\n")

        result, completed = self.run_assessment("address", result_name="mismatch")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("workspace must match", completed.stderr)

    def test_malformed_review_evidence_is_refused_before_result_creation(self):
        self.write_json(self.review / "input.json", [])

        result, completed = self.run_assessment("address")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("invalid Review evidence", completed.stderr)

    def test_sibling_owned_finding_is_out_of_scope_under_trusted_policy(self):
        records = {
            "task": {"id": "task", "title": "Change the API", "parent": "epic"},
            "epic": {
                "id": "epic",
                "title": "API epic",
                "children": ["task", "callers"],
            },
            "callers": {
                "id": "callers",
                "title": "Migrate callers",
                "description": "Caller migration is owned by this sibling.",
            },
        }
        raw, facts = build_snapshot(records["task"], records.__getitem__)
        snapshot = self.root / "related-work.jsonl"
        snapshot.write_bytes(raw)
        input_path, result, environment = self.prepare_assessment(
            "sibling-owned-finding"
        )
        value = json.loads(input_path.read_text())
        value["related_work"] = reference(snapshot, facts)
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        decision = json.loads((result / "output.json").read_text())["assessment"][
            "decisions"
        ][0]
        self.assertFalse(decision["worth_addressing"])

    def test_change_objective_is_required_and_given_to_the_assessor(self):
        marker = self.root / "prompt.txt"
        input_path, result, environment = self.prepare_assessment(
            "capture-prompt",
            command=[
                sys.executable,
                str(FIXTURE),
                "capture-prompt",
                str(marker),
            ],
        )

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Make the reviewed implementation correct.", marker.read_text())

        change = json.loads((self.change / "output.json").read_text())
        change["change"]["objective"] = ""
        self.write_json(self.change / "output.json", change)
        result, completed = self.run_assessment(
            "address", result_name="missing-objective"
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("objective", completed.stderr)

    def test_malformed_review_repository_state_is_refused_without_traceback(self):
        original = json.loads((self.review / "output.json").read_text())
        for field in ("head", "dirty", "status"):
            with self.subTest(field=field):
                malformed = json.loads(json.dumps(original))
                malformed["repository"]["after"].pop(field)
                self.write_json(self.review / "output.json", malformed)

                result, completed = self.run_assessment(
                    "address", result_name=f"missing-{field}"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())
                self.assertNotIn("Traceback", completed.stderr)
                self.assertIn("invalid Review evidence", completed.stderr)

    def test_agent_and_structured_protocol_failures_are_sealed(self):
        for scenario in ("invalid-events", "invalid-json"):
            with self.subTest(scenario=scenario):
                result, completed = self.run_assessment(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["assessment"])

    def test_agent_launch_and_workspace_observation_failures_are_sealed(self):
        input_path, result, environment = self.prepare_assessment(
            "unused", command=[str(self.root / "missing-agent")]
        )
        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertNotEqual(output["process"]["exit_code"], 0)
        self.assertIsNone(output["agent"])

        result, completed = self.run_assessment(
            "mutate-workspace", result_name="mutated"
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertFalse(output["repository"]["unchanged"])

    def test_post_assessment_git_damage_is_sealed(self):
        result, completed = self.run_assessment("damage-git")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIn("observation_error", output["repository"])

    def test_timeout_and_interrupt_terminate_the_agent_process_group(self):
        marker = self.root / "timed-out-descendant.pid"
        input_path, result, environment = self.prepare_assessment(
            "hang", command=[sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 1
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "timed_out",
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

        marker = self.root / "interrupted-descendant.pid"
        input_path, result, environment = self.prepare_assessment(
            "hang",
            result_name="interrupted",
            command=[sys.executable, str(FIXTURE), "hang", str(marker)],
        )
        assessor = subprocess.Popen(
            [sys.executable, "-m", "afk_assess", str(input_path), str(result)],
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
        assessor.send_signal(signal.SIGINT)
        _stdout, stderr = assessor.communicate(timeout=5)

        self.assertEqual(assessor.returncode, 1, stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "interrupted",
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_closed_progress_stdout_does_not_prevent_sealing(self):
        input_path, result, environment = self.prepare_assessment("delayed-address")
        assessor = subprocess.Popen(
            [sys.executable, "-m", "afk_assess", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert assessor.stdout is not None
        for line in assessor.stdout:
            if "starting finding-assessment agent" in line:
                break
        else:
            self.fail("finding-assessment agent did not start")
        assessor.stdout.close()
        assert assessor.stderr is not None
        stderr = assessor.stderr.read()
        assessor.stderr.close()

        self.assertEqual(assessor.wait(timeout=5), 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "completed",
        )

    def finding(self, title):
        return {
            "severity": "medium",
            "title": title,
            "details": "The fixture reports one concrete problem.",
            "locations": [{"path": "README.md", "line": 1}],
        }

    def set_findings(self, findings):
        output = json.loads((self.review / "output.json").read_text())
        output["review"]["findings"] = findings
        self.write_json(self.review / "output.json", output)

    def run_assessment(self, scenario, result_name="assessment"):
        input_path, result, environment = self.prepare_assessment(scenario, result_name)
        return result, self.invoke(input_path, result, environment)

    def prepare_assessment(self, scenario, result_name="assessment", command=None):
        assessment_input = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "review_directory": str(self.review),
            "timeout_seconds": 5,
        }
        input_path = self.root / "assessment.json"
        self.write_json(input_path, assessment_input)
        result = self.root / result_name
        environment = os.environ.copy()
        bin_directory = self.root / "bin"
        bin_directory.mkdir(exist_ok=True)
        if command is None:
            install_pi(bin_directory, FIXTURE, scenario)
        else:
            executable = bin_directory / "pi"
            rendered = " ".join(shlex.quote(item) for item in command)
            executable.write_text(f'#!/bin/sh\nexec {rendered} "$@"\n')
            executable.chmod(0o755)
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        return input_path, result, environment

    def invoke(self, input_path, result, environment):
        return subprocess.run(
            [sys.executable, "-m", "afk_assess", str(input_path), str(result)],
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
