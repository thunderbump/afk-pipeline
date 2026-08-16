import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PublicIterationPolicyCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_iterate", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "usage: python3 -m afk_iterate POLICY_JSON RESULT_DIRECTORY",
            help_result.stdout,
        )
        self.assertIn("latest completed Finding Assessment", help_result.stdout)

        invalid = subprocess.run(
            [sys.executable, "-m", "afk_iterate", "policy.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage: python3 -m afk_iterate", invalid.stderr)


class IterationPolicyCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("before\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Before")
        self.before = self.state()
        (self.workspace / "README.md").write_text("implementation\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Implementation")
        self.implementation = self.state()
        (self.root / "02-validation").mkdir()
        self.assessment = self.make_assessment(worth_addressing=False)

    def test_no_actionable_findings_stop_without_using_remaining_budget(self):
        result, completed = self.run_policy(max_responses=3)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads((result / "input.json").read_text()),
            {
                "schema_version": 1,
                "assessment_directory": str(self.assessment),
                "max_responses": 3,
            },
        )
        self.assertEqual(
            json.loads((result / "output.json").read_text()),
            {
                "schema_version": 1,
                "outcome": "completed",
                "policy": {
                    "decision": "stop",
                    "completed_responses": 0,
                    "max_responses": 3,
                    "actionable_findings": 0,
                    "reason": "the latest assessment has no actionable findings",
                },
            },
        )
        self.assertFalse((result / "output.json.tmp").exists())

    def test_response_lineage_exhausts_or_continues_at_the_caller_limit(self):
        self.assessment = self.make_response_assessment(worth_addressing=True)

        exhausted_result, exhausted = self.run_policy(max_responses=1)
        continue_result, continued = self.run_policy(max_responses=2)

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        exhausted_policy = json.loads((exhausted_result / "output.json").read_text())[
            "policy"
        ]
        self.assertEqual(
            exhausted_policy,
            {
                "decision": "exhausted",
                "completed_responses": 1,
                "max_responses": 1,
                "actionable_findings": 1,
                "reason": "the response limit has been reached",
            },
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        continued_policy = json.loads((continue_result / "output.json").read_text())[
            "policy"
        ]
        self.assertEqual(
            continued_policy,
            {
                "decision": "continue",
                "completed_responses": 1,
                "max_responses": 2,
                "actionable_findings": 1,
                "next_response_number": 2,
                "reason": "actionable findings remain within the response limit",
            },
        )

    def test_invalid_input_and_failed_assessment_are_refused_before_results(self):
        invalid_inputs = [
            {
                "schema_version": 1,
                "assessment_directory": str(self.assessment),
                "max_responses": -1,
            },
            {
                "schema_version": 1,
                "assessment_directory": "relative/assessment",
                "max_responses": 1,
            },
            {
                "schema_version": 1,
                "assessment_directory": str(self.assessment),
                "max_responses": 1,
                "unexpected": True,
            },
        ]
        for index, value in enumerate(invalid_inputs):
            with self.subTest(index=index):
                result, completed = self.run_input(value, f"invalid-{index}")
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())

        assessment_output = json.loads((self.assessment / "output.json").read_text())
        assessment_output["outcome"] = "failed"
        self.write_json(self.assessment / "output.json", assessment_output)
        result, completed = self.run_policy(max_responses=1)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("completed Finding Assessment", completed.stderr)
        self.assertFalse(result.exists())

    def test_recursive_evidence_cycle_is_refused_before_result_creation(self):
        self.assessment = self.make_response_assessment(worth_addressing=True)
        latest_review_input = json.loads(
            (self.root / "09-review" / "input.json").read_text()
        )
        latest_change = latest_review_input["change_directory"]
        prior_review_path = Path(
            json.loads((self.root / "05-assessment" / "input.json").read_text())[
                "review_directory"
            ]
        )
        prior_review_input = json.loads((prior_review_path / "input.json").read_text())
        prior_review_input["change_directory"] = latest_change
        self.write_json(prior_review_path / "input.json", prior_review_input)

        result, completed = self.run_policy(max_responses=2)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("cycle", completed.stderr)
        self.assertFalse(result.exists())

    def test_malformed_assessment_and_review_inputs_are_refused(self):
        assessment_input_path = self.assessment / "input.json"
        original_assessment = json.loads(assessment_input_path.read_text())
        invalid_assessment = {**original_assessment, "schema_version": 2}
        self.write_json(assessment_input_path, invalid_assessment)
        result, completed = self.run_input(
            {
                "schema_version": 1,
                "assessment_directory": str(self.assessment),
                "max_responses": 1,
            },
            "bad-assessment",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

        self.write_json(assessment_input_path, original_assessment)
        review_path = Path(original_assessment["review_directory"])
        review_input_path = review_path / "input.json"
        review_input = json.loads(review_input_path.read_text())
        review_input["schema_version"] = 2
        self.write_json(review_input_path, review_input)
        result, completed = self.run_input(
            {
                "schema_version": 1,
                "assessment_directory": str(self.assessment),
                "max_responses": 1,
            },
            "bad-review",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

    def test_result_directory_cannot_modify_workspace_or_lineage_evidence(self):
        self.assessment = self.make_response_assessment(worth_addressing=True)
        value = {
            "schema_version": 1,
            "assessment_directory": str(self.assessment),
            "max_responses": 1,
        }
        input_path = self.root / "policy-protected.json"
        self.write_json(input_path, value)
        protected = [
            self.workspace,
            self.assessment,
            self.root / "09-review",
            self.root / "08-change",
            self.root / "07-validation",
            self.root / "06-response",
            self.root / "05-assessment",
            self.root / "04-review",
            self.root / "03-change",
            self.root / "02-validation",
            self.root / "01-attempt",
        ]
        for index, directory in enumerate(protected):
            with self.subTest(directory=directory):
                result = directory / f"iteration-policy-{index}"
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "afk_iterate",
                        str(input_path),
                        str(result),
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("outside the workspace and evidence", completed.stderr)
                self.assertFalse(result.exists())

    def test_existing_result_directory_is_not_replaced(self):
        result = self.root / "result-1"
        result.mkdir()
        sentinel = result / "keep"
        sentinel.write_text("preserve me\n")

        _, completed = self.run_policy(max_responses=1)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(sentinel.read_text(), "preserve me\n")
        self.assertFalse((result / "input.json").exists())

    def make_response_assessment(self, worth_addressing):
        initial_output = json.loads((self.assessment / "output.json").read_text())
        initial_output["assessment"]["decisions"][0]["worth_addressing"] = True
        self.write_json(self.assessment / "output.json", initial_output)

        response = self.root / "06-response"
        response.mkdir()
        self.write_json(
            response / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "assessment_directory": str(self.assessment),
                "timeout_seconds": 60,
            },
        )
        (self.workspace / "README.md").write_text("repaired implementation\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Respond to feedback")
        response_state = self.state()
        self.write_json(
            response / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "response": {
                    "summary": "Addressed the finding.",
                    "finding_responses": [
                        {"finding_index": 0, "response": "Handled the edge case."}
                    ],
                },
                "repository": {
                    "before": self.implementation,
                    "after": response_state,
                    "commits_between_heads": [response_state["head"]],
                    "descends_from_before": True,
                },
            },
        )
        change = self.root / "08-change"
        change.mkdir()
        self.write_json(
            change / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "objective": "Make the implementation correct.",
                    "workspace": str(self.workspace),
                    "repository": {
                        "before": self.implementation,
                        "after": response_state,
                    },
                    "source": {
                        "kind": "feedback_response",
                        "directory": str(response),
                    },
                },
            },
        )
        (self.root / "07-validation").mkdir()
        review = self.root / "09-review"
        review.mkdir()
        self.write_json(
            review / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "change_directory": str(change),
                "validation_directory": str(self.root / "07-validation"),
                "timeout_seconds": 60,
            },
        )
        finding = {
            "severity": "medium",
            "title": "Check the response",
            "details": "The response may miss another edge case.",
            "locations": [{"path": "README.md", "line": 1}],
        }
        self.write_json(
            review / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "review": {"summary": "One finding.", "findings": [finding]},
                "repository": {
                    "before": response_state,
                    "after": response_state,
                    "unchanged": True,
                },
            },
        )
        assessment = self.root / "10-assessment"
        assessment.mkdir()
        self.write_json(
            assessment / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "review_directory": str(review),
                "timeout_seconds": 60,
            },
        )
        self.write_json(
            assessment / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {
                    "summary": "Assessment complete.",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "worth_addressing": worth_addressing,
                            "rationale": "The finding was assessed.",
                        }
                    ],
                },
                "repository": {
                    "before": response_state,
                    "after": response_state,
                    "unchanged": True,
                },
            },
        )
        return assessment

    def make_assessment(self, worth_addressing):
        attempt = self.root / "01-attempt"
        attempt.mkdir()
        self.write_json(
            attempt / "input.json",
            {
                "schema_version": 1,
                "objective": "Make the implementation correct.",
                "workspace": str(self.workspace),
                "command": ["agent"],
                "timeout_seconds": 60,
            },
        )
        self.write_json(
            attempt / "output.json",
            {
                "schema_version": 1,
                "outcome": "succeeded",
                "repository": {
                    "before": self.before,
                    "after": self.implementation,
                    "commits_between_heads": [self.implementation["head"]],
                },
            },
        )
        change = self.root / "03-change"
        change.mkdir()
        self.write_json(
            change / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "objective": "Make the implementation correct.",
                    "workspace": str(self.workspace),
                    "repository": {
                        "before": self.before,
                        "after": self.implementation,
                    },
                    "source": {"kind": "attempt", "directory": str(attempt)},
                },
            },
        )
        review = self.root / "04-review"
        review.mkdir()
        self.write_json(
            review / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "change_directory": str(change),
                "validation_directory": str(self.root / "02-validation"),
                "timeout_seconds": 60,
            },
        )
        finding = {
            "severity": "medium",
            "title": "Check the implementation",
            "details": "The implementation may miss an edge case.",
            "locations": [{"path": "README.md", "line": 1}],
        }
        self.write_json(
            review / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "review": {"summary": "One finding.", "findings": [finding]},
                "repository": {
                    "before": self.implementation,
                    "after": self.implementation,
                    "unchanged": True,
                },
            },
        )
        assessment = self.root / "05-assessment"
        assessment.mkdir()
        self.write_json(
            assessment / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "review_directory": str(review),
                "timeout_seconds": 60,
            },
        )
        self.write_json(
            assessment / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {
                    "summary": "Assessment complete.",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "worth_addressing": worth_addressing,
                            "rationale": "The finding was assessed.",
                        }
                    ],
                },
                "repository": {
                    "before": self.implementation,
                    "after": self.implementation,
                    "unchanged": True,
                },
            },
        )
        return assessment

    def run_policy(self, max_responses):
        value = {
            "schema_version": 1,
            "assessment_directory": str(self.assessment),
            "max_responses": max_responses,
        }
        return self.run_input(value, str(max_responses))

    def run_input(self, value, name):
        input_path = self.root / f"policy-{name}.json"
        self.write_json(input_path, value)
        result = self.root / f"result-{name}"
        completed = subprocess.run(
            [sys.executable, "-m", "afk_iterate", str(input_path), str(result)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, completed

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
