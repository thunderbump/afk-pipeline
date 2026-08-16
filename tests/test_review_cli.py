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
FIXTURE = ROOT / "tests" / "fixture_review_agent.py"


class PublicReviewCliTest(unittest.TestCase):
    def test_help_exits_zero_and_describes_arguments(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_review", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "usage: python3 -m afk_review REVIEW_JSON RESULT_DIRECTORY",
            completed.stdout,
        )
        self.assertIn("review JSON", completed.stdout)
        self.assertIn("RESULT_DIRECTORY", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_wrong_number_of_ordinary_arguments_exits_two(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_review", "review.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "usage: python3 -m afk_review REVIEW_JSON RESULT_DIRECTORY",
            completed.stderr,
        )


class ReviewCliTest(unittest.TestCase):
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
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "note.txt").write_text("tracked directory\n")
        self.git("add", "README.md", "docs/note.txt")
        self.git("commit", "--quiet", "-m", "Initial state")
        self.before = self.state()
        (self.workspace / "README.md").write_text("after\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Implementation")
        self.after = self.state()
        self.change = self.root / "committed-change"
        self.change.mkdir()
        self.write_json(
            self.change / "input.json",
            {
                "schema_version": 1,
                "source": {
                    "kind": "attempt",
                    "directory": str(self.root / "attempt"),
                },
            },
        )
        self.write_json(
            self.change / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "objective": "Change before to after.",
                    "workspace": str(self.workspace),
                    "repository": {"before": self.before, "after": self.after},
                    "source": {
                        "kind": "attempt",
                        "directory": str(self.root / "attempt"),
                    },
                },
            },
        )
        self.validation = self.root / "validation"
        self.validation.mkdir()
        self.write_json(
            self.validation / "output.json",
            {
                "schema_version": 1,
                "outcome": "passed",
                "repository": {"before": self.after, "after": self.after},
            },
        )

    def test_completed_review_seals_structured_output_and_raw_artifacts(self):
        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        for line in completed.stdout.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T.*Z ")
        self.assertEqual(
            [line.split(" ", 1)[1] for line in completed.stdout.splitlines()],
            [
                "loading review input",
                "review input accepted",
                "loading Committed Change and Validation evidence",
                "observing reviewed repository",
                "preparing review result directory",
                (
                    "starting review agent "
                    f"(timeout=5s; artifacts: events={result / 'events.jsonl'}, "
                    f"stderr={result / 'stderr.log'})"
                ),
                "review agent completed",
                "observing repository after review",
                f"sealed completed review outcome at {result / 'output.json'}",
            ],
        )
        self.assertNotIn("No actionable defects", completed.stdout)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["process"], {"exit_code": 0, "signal": None})
        self.assertEqual(output["agent"], {"status": "completed"})
        self.assertEqual(
            output["review"],
            {"summary": "No actionable defects found.", "findings": []},
        )
        self.assertEqual(output["repository"]["before"], self.after)
        self.assertEqual(output["repository"]["after"], self.after)
        self.assertTrue(output["repository"]["unchanged"])
        self.assertEqual(output["artifacts"]["diff"], "diff.patch")
        diff = (result / "diff.patch").read_text()
        self.assertIn("-before", diff)
        self.assertIn("+after", diff)
        self.assertTrue((result / "events.jsonl").is_file())
        self.assertEqual((result / "stderr.log").read_text(), "")
        self.assertFalse((result / "output.json.tmp").exists())

    def test_feedback_response_change_uses_the_same_review_interface(self):
        change = json.loads((self.change / "output.json").read_text())
        change["change"]["source"] = {
            "kind": "feedback_response",
            "directory": str(self.root / "response"),
        }
        self.write_json(self.change / "output.json", change)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads((result / "input.json").read_text())["change_directory"],
            str(self.change),
        )
        self.assertIn("-before", (result / "diff.patch").read_text())

    def test_findings_are_completed_and_require_line_anchors(self):
        result, completed = self.run_review("findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(
            output["review"]["findings"][0]["locations"],
            [{"path": "README.md", "line": 1}],
        )

        result, completed = self.run_review("missing-line", result_name="invalid")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["review"])
        self.assertIn("line must be an integer", output["review_error"])

    def test_finding_locations_must_exist_within_the_reviewed_head(self):
        (self.workspace / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (self.workspace / "ignored.txt").write_text("not in HEAD\n")
        for scenario in (
            "missing-path",
            "outside-path",
            "ignored-path",
            "directory-path",
            "bad-line",
        ):
            with self.subTest(scenario=scenario):
                result, completed = self.run_review(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["review"])
                self.assertIn("finding location", output["review_error"])

    def test_workspace_must_match_the_validated_change_before_review(self):
        (self.workspace / "unreviewed.txt").write_text("different state\n")

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("workspace must match", completed.stderr)

    def test_dirty_implementation_evidence_is_refused_before_review(self):
        (self.workspace / "uncommitted.txt").write_text("not committed\n")
        dirty = self.state()
        change = json.loads((self.change / "output.json").read_text())
        change["change"]["repository"]["after"] = dirty
        self.write_json(self.change / "output.json", change)
        validation = json.loads((self.validation / "output.json").read_text())
        validation["repository"]["before"] = dirty
        validation["repository"]["after"] = dirty
        self.write_json(self.validation / "output.json", validation)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("repository states must be clean", completed.stderr)

    def test_detached_workspace_at_the_validated_head_is_reviewable(self):
        self.git("checkout", "--quiet", "--detach")

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertIsNone(output["repository"]["before"]["branch"])
        self.assertTrue(output["repository"]["unchanged"])

    def test_reviewer_workspace_mutation_is_a_sealed_failure(self):
        result, completed = self.run_review("mutate-workspace")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertFalse(output["repository"]["unchanged"])
        self.assertTrue(output["repository"]["after"]["dirty"])

    def test_agent_protocol_and_post_review_observation_failures_are_sealed(self):
        result, completed = self.run_review("invalid-events")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["agent"]["status"], "error")

        result, completed = self.run_review("damage-git", result_name="damaged-git")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIn("observation_error", output["repository"])

    def test_timeout_terminates_the_agent_process_group_and_seals_output(self):
        marker = self.root / "descendant.pid"

        result, completed = self.run_review(
            "hang", timeout_seconds=1, extra_args=[str(marker)]
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        descendant = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_agent_launch_failure_is_sealed(self):
        result, completed = self.run_review(
            "unused", command=[str(self.root / "missing-agent")]
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["process"]["exit_code"])
        self.assertIn("missing-agent", output["process"]["error"])
        self.assertIsNone(output["agent"])

    def test_interrupt_terminates_the_agent_process_group_and_seals_output(self):
        marker = self.root / "interrupted-descendant.pid"
        input_path, result, environment = self.prepare_review(
            "hang", timeout_seconds=30, extra_args=[str(marker)]
        )
        reviewer = subprocess.Popen(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
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

        reviewer.send_signal(signal.SIGINT)
        _stdout, stderr = reviewer.communicate(timeout=5)

        self.assertEqual(reviewer.returncode, 1, stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "interrupted",
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_closed_progress_stdout_does_not_prevent_sealing(self):
        input_path, result, environment = self.prepare_review("delayed-no-findings")
        reviewer = subprocess.Popen(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert reviewer.stdout is not None
        for line in reviewer.stdout:
            if "starting review agent" in line:
                break
        else:
            self.fail("review agent did not start")
        reviewer.stdout.close()
        assert reviewer.stderr is not None
        stderr = reviewer.stderr.read()
        reviewer.stderr.close()

        self.assertEqual(reviewer.wait(timeout=5), 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "completed",
        )

    def test_invalid_input_and_existing_result_are_refused(self):
        input_path, result, environment = self.prepare_review("no-findings")
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 0
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

        input_path, result, environment = self.prepare_review("no-findings")
        result.mkdir()
        sentinel = result / "keep.txt"
        sentinel.write_text("caller data\n")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual([sentinel], list(result.iterdir()))

    def test_attempt_directory_is_not_a_supported_review_input(self):
        input_path, result, environment = self.prepare_review("no-findings")
        value = json.loads(input_path.read_text())
        value["attempt_directory"] = value.pop("change_directory")
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("change_directory", completed.stderr)
        self.assertFalse(result.exists())

    def test_failed_and_mismatched_change_or_validation_are_refused(self):
        original_change = json.loads((self.change / "output.json").read_text())
        original_validation = json.loads((self.validation / "output.json").read_text())
        cases = {
            "failed-change": (
                lambda change, _validation: change.update(outcome="failed"),
                "Committed Change must have completed",
            ),
            "failed-validation": (
                lambda _change, validation: validation.update(outcome="failed"),
                "Validation must have passed",
            ),
            "wrong-validation-before": (
                lambda _change, validation: validation["repository"].update(
                    before=self.before
                ),
                "identify one repository state",
            ),
            "dirty-change-before": (
                lambda change, _validation: change["change"]["repository"][
                    "before"
                ].update(dirty=True, status=[" M README.md"]),
                "repository states must be clean",
            ),
        }
        for name, (mutate, error) in cases.items():
            with self.subTest(name=name):
                change = json.loads(json.dumps(original_change))
                validation = json.loads(json.dumps(original_validation))
                mutate(change, validation)
                self.write_json(self.change / "output.json", change)
                self.write_json(self.validation / "output.json", validation)

                result, completed = self.run_review(
                    "no-findings", result_name=f"review-{name}"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(error, completed.stderr)
                self.assertFalse(result.exists())

    def test_noncanonical_or_missing_change_heads_are_refused_before_results(self):
        original = json.loads((self.change / "output.json").read_text())
        for name, head in (
            ("symbolic", "HEAD~1"),
            ("missing", "0000000000000000000000000000000000000000"),
        ):
            with self.subTest(name=name):
                change = json.loads(json.dumps(original))
                change["change"]["repository"]["before"]["head"] = head
                self.write_json(self.change / "output.json", change)

                result, completed = self.run_review(
                    "no-findings", result_name=f"review-{name}-head"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn("canonical commit object IDs", completed.stderr)
                self.assertFalse(result.exists())

    def test_malformed_evidence_is_refused_before_result_creation(self):
        self.write_json(self.change / "output.json", {"outcome": "completed"})

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("Committed Change must use schema_version 1", completed.stderr)

    def test_committed_change_provenance_is_required(self):
        change = json.loads((self.change / "output.json").read_text())
        del change["change"]["source"]
        self.write_json(self.change / "output.json", change)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("Committed Change source is invalid", completed.stderr)

    def test_validation_schema_is_required(self):
        validation = json.loads((self.validation / "output.json").read_text())
        del validation["schema_version"]
        self.write_json(self.validation / "output.json", validation)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("Validation must use schema_version 1", completed.stderr)

    def run_review(
        self,
        scenario,
        result_name="review",
        timeout_seconds=5,
        extra_args=None,
        command=None,
    ):
        input_path, result, environment = self.prepare_review(
            scenario,
            result_name=result_name,
            timeout_seconds=timeout_seconds,
            extra_args=extra_args,
            command=command,
        )
        return result, self.invoke(input_path, result, environment)

    def prepare_review(
        self,
        scenario,
        result_name="review",
        timeout_seconds=5,
        extra_args=None,
        command=None,
    ):
        review = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "change_directory": str(self.change),
            "validation_directory": str(self.validation),
            "timeout_seconds": timeout_seconds,
        }
        input_path = self.root / "review.json"
        self.write_json(input_path, review)
        result = self.root / result_name
        environment = os.environ.copy()
        environment["AFK_REVIEW_AGENT_COMMAND"] = json.dumps(
            command or [sys.executable, str(FIXTURE), scenario, *(extra_args or [])]
        )
        return input_path, result, environment

    def invoke(self, input_path, result, environment):
        return subprocess.run(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
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
