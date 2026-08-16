import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PublicChangeCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_change", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "usage: python3 -m afk_change SOURCE_JSON RESULT_DIRECTORY",
            help_result.stdout,
        )
        self.assertIn("committed-change source JSON", help_result.stdout)

        invalid = subprocess.run(
            [sys.executable, "-m", "afk_change", "source.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("usage: python3 -m afk_change", invalid.stderr)


class ChangeCliTest(unittest.TestCase):
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

        self.attempt = self.root / "01-attempt"
        self.attempt.mkdir()
        self.write_json(
            self.attempt / "input.json",
            {
                "schema_version": 1,
                "objective": "Make the implementation correct.",
                "workspace": str(self.workspace),
                "command": ["agent"],
                "timeout_seconds": 60,
            },
        )
        self.write_json(
            self.attempt / "output.json",
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

    def test_succeeded_attempt_projects_one_committed_change(self):
        result, completed = self.run_change("attempt", self.attempt)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(
            output["change"],
            {
                "objective": "Make the implementation correct.",
                "workspace": str(self.workspace),
                "repository": {
                    "before": self.before,
                    "after": self.implementation,
                },
                "source": {"kind": "attempt", "directory": str(self.attempt)},
            },
        )
        self.assertEqual(
            json.loads((result / "input.json").read_text()),
            {
                "schema_version": 1,
                "source": {"kind": "attempt", "directory": str(self.attempt)},
            },
        )
        self.assertFalse((result / "output.json.tmp").exists())

    def test_projection_does_not_require_the_final_head_to_be_checked_out(self):
        self.git("checkout", "--quiet", self.before["head"])

        result, completed = self.run_change("attempt", self.attempt)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["change"]["repository"]["after"], self.implementation)

    def test_completed_feedback_response_projects_the_response_commit(self):
        response = self.make_feedback_response()

        result, completed = self.run_change("feedback_response", response)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        change = json.loads((result / "output.json").read_text())["change"]
        self.assertEqual(change["objective"], "Make the implementation correct.")
        self.assertEqual(change["repository"]["before"], self.implementation)
        self.assertEqual(change["repository"]["after"], self.response_state)
        self.assertEqual(
            change["source"],
            {"kind": "feedback_response", "directory": str(response)},
        )

    def test_rejects_failed_dirty_unchanged_and_mismatched_attempt_evidence(self):
        cases = {
            "failed": lambda value: value.update(outcome="failed"),
            "dirty": lambda value: value["repository"]["after"].update(
                dirty=True, status=[" M README.md"]
            ),
            "unchanged": lambda value: value["repository"].update(
                after=self.before, commits_between_heads=[]
            ),
            "wrong-range": lambda value: value["repository"].update(
                commits_between_heads=[self.before["head"]]
            ),
        }
        original = json.loads((self.attempt / "output.json").read_text())
        for name, mutate in cases.items():
            with self.subTest(name=name):
                value = json.loads(json.dumps(original))
                mutate(value)
                self.write_json(self.attempt / "output.json", value)
                result, completed = self.run_change(
                    "attempt", self.attempt, f"result-{name}"
                )
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())

    def test_rejects_a_mismatched_feedback_response_evidence_chain(self):
        response = self.make_feedback_response()
        assessment_input = json.loads((self.assessment / "input.json").read_text())
        assessment_input["workspace"] = str(self.root / "other-workspace")
        self.write_json(self.assessment / "input.json", assessment_input)

        result, completed = self.run_change("feedback_response", response)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("workspaces must match", completed.stderr)
        self.assertFalse(result.exists())

    def test_invalid_input_and_existing_result_do_not_replace_evidence(self):
        result, invalid = self.run_change("unknown", self.attempt)
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse(result.exists())

        result.mkdir()
        sentinel = result / "keep"
        sentinel.write_text("preserve me\n")
        _, existing = self.run_change("attempt", self.attempt)
        self.assertEqual(existing.returncode, 2)
        self.assertEqual(sentinel.read_text(), "preserve me\n")
        self.assertFalse((result / "input.json").exists())

    def make_feedback_response(self):
        review = self.root / "04-review"
        review.mkdir()
        self.write_json(
            review / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "attempt_directory": str(self.attempt),
                "validation_directory": str(self.root / "02-validation"),
                "timeout_seconds": 60,
            },
        )
        finding = {
            "severity": "medium",
            "title": "Address the edge case",
            "details": "The implementation misses a concrete edge case.",
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
        self.assessment = self.root / "05-assessment"
        self.assessment.mkdir()
        self.write_json(
            self.assessment / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "review_directory": str(review),
                "timeout_seconds": 60,
            },
        )
        self.write_json(
            self.assessment / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {
                    "summary": "Address it.",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "worth_addressing": True,
                            "rationale": "It is concrete.",
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
        self.response_state = self.state()
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
                    "after": self.response_state,
                    "commits_between_heads": [self.response_state["head"]],
                    "descends_from_before": True,
                },
            },
        )
        return response

    def run_change(self, kind, directory, result_name="03-committed-change"):
        source = {
            "schema_version": 1,
            "source": {"kind": kind, "directory": str(directory)},
        }
        input_path = self.root / "source.json"
        self.write_json(input_path, source)
        result = self.root / result_name
        completed = subprocess.run(
            [sys.executable, "-m", "afk_change", str(input_path), str(result)],
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
