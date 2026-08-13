import json
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_agent.py"


class PublicCliTest(unittest.TestCase):
    def test_help_exits_zero_and_describes_arguments(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("usage: python3 -m afk_attempt ASSIGNMENT_JSON ATTEMPT_DIRECTORY", completed.stdout)
        self.assertIn("assignment JSON", completed.stdout)
        self.assertIn("ATTEMPT_DIRECTORY", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_wrong_number_of_ordinary_arguments_exits_two(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", "assignment.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage: python3 -m afk_attempt ASSIGNMENT_JSON ATTEMPT_DIRECTORY", completed.stderr)


class AttemptExecutorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("fixture repository\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Initial state")

    def test_successful_attempt_seals_input_output_and_logs(self):
        assignment = {
            "schema_version": 1,
            "objective": "Exercise the success path.",
            "workspace": str(self.workspace),
            "command": [sys.executable, str(FIXTURE), "success"],
            "timeout_seconds": 5,
        }
        assignment_path = self.root / "assignment.json"
        assignment_path.write_text(json.dumps(assignment))
        attempt = self.root / "attempt"

        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", str(assignment_path), str(attempt)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads((attempt / "input.json").read_text()), assignment)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "succeeded")
        self.assertEqual(output["process"], {"exit_code": 0, "signal": None})
        self.assertEqual(output["agent"], {"status": "completed"})
        self.assertEqual(output["repository"]["before"], output["repository"]["after"])
        self.assertEqual(output["repository"]["before"]["branch"], "main")
        self.assertEqual(output["repository"]["commits_between_heads"], [])
        self.assertTrue((attempt / "events.jsonl").is_file())
        self.assertTrue((attempt / "stderr.log").is_file())
        self.assertFalse((attempt / "output.json.tmp").exists())
        self.assertGreaterEqual(
            (attempt / "output.json").stat().st_mtime_ns,
            (attempt / "events.jsonl").stat().st_mtime_ns,
        )

    def test_agent_error_fails_even_when_process_exits_zero(self):
        attempt, completed = self.run_attempt("agent-error")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["process"]["exit_code"], 0)
        self.assertEqual(output["agent"], {"status": "error", "error": "fixture agent error"})

    def test_aborted_agent_fails_even_when_process_exits_zero(self):
        attempt, completed = self.run_attempt("agent-aborted")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["agent"], {"status": "aborted"})

    def test_process_failure_is_sealed_with_stderr(self):
        attempt, completed = self.run_attempt("process-failure")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["process"], {"exit_code": 7, "signal": None})
        self.assertIn("fixture process failed", (attempt / "stderr.log").read_text())

    def test_invalid_agent_events_are_sealed_as_failure(self):
        attempt, completed = self.run_attempt("invalid-events")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["agent"], {"status": "error", "error": "invalid agent event JSON"})

    def test_malformed_agent_shapes_and_encoding_are_sealed_as_failure(self):
        for scenario in ("invalid-event-shape", "invalid-event-encoding"):
            with self.subTest(scenario=scenario):
                attempt, completed = self.run_attempt(scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((attempt / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertEqual(output["agent"]["status"], "error")

    def test_agent_end_must_close_the_event_stream(self):
        attempt, completed = self.run_attempt("events-after-end")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["agent"], {"status": "error", "error": "events follow agent_end"})

    def test_post_run_git_observation_failure_is_sealed(self):
        attempt, completed = self.run_attempt("damage-git")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIn("observation_error", output["repository"])

    def test_commit_range_observation_failure_is_sealed(self):
        attempt, completed = self.run_attempt("damage-history")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNotNone(output["repository"]["after"])
        self.assertIsNone(output["repository"]["commits_between_heads"])
        self.assertIn("observation_error", output["repository"])

    def test_runner_launch_failure_is_sealed(self):
        assignment = {
            "schema_version": 1,
            "objective": "Exercise launch failure.",
            "workspace": str(self.workspace),
            "command": [str(self.root / "missing-runner")],
            "timeout_seconds": 5,
        }
        assignment_path = self.root / "launch-failure.json"
        assignment_path.write_text(json.dumps(assignment))
        attempt = self.root / "attempt-launch-failure"

        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", str(assignment_path), str(attempt)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["process"]["exit_code"])
        self.assertIn("missing-runner", output["process"]["error"])
        self.assertIsNone(output["agent"])

    def test_timeout_terminates_the_process_group_and_seals_output(self):
        marker = self.root / "descendant.pid"
        attempt, completed = self.run_attempt("hang", timeout_seconds=1, extra_args=[str(marker)])

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        self.assertTrue(marker.is_file())
        descendant = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_interrupt_terminates_the_process_group_and_seals_output(self):
        marker = self.root / "interrupted-descendant.pid"
        assignment = {
            "schema_version": 1,
            "objective": "Exercise interruption.",
            "workspace": str(self.workspace),
            "command": [sys.executable, str(FIXTURE), "hang", str(marker)],
            "timeout_seconds": 30,
        }
        assignment_path = self.root / "interrupt.json"
        assignment_path.write_text(json.dumps(assignment))
        attempt = self.root / "attempt-interrupt"
        executor = subprocess.Popen(
            [sys.executable, "-m", "afk_attempt", str(assignment_path), str(attempt)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(marker.is_file())
        executor.send_signal(signal.SIGINT)
        stdout, stderr = executor.communicate(timeout=5)

        self.assertEqual(executor.returncode, 1, stderr)
        self.assertEqual(json.loads((attempt / "output.json").read_text())["outcome"], "interrupted")
        descendant = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_existing_attempt_directory_is_refused_without_changes(self):
        attempt = self.root / "attempt-success"
        attempt.mkdir()
        sentinel = attempt / "keep.txt"
        sentinel.write_text("caller data\n")

        returned_attempt, completed = self.run_attempt("success")

        self.assertEqual(returned_attempt, attempt)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(sentinel.read_text(), "caller data\n")
        self.assertEqual([sentinel], list(attempt.iterdir()))

    def test_git_branch_and_created_commit_are_observed(self):
        before = self.git("rev-parse", "HEAD")
        attempt, completed = self.run_attempt("git-commit")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((attempt / "output.json").read_text())
        self.assertEqual(output["repository"]["before"]["head"], before)
        self.assertEqual(output["repository"]["before"]["branch"], "main")
        self.assertNotEqual(output["repository"]["after"]["head"], before)
        self.assertEqual(output["repository"]["after"]["branch"], "main")
        self.assertEqual(
            output["repository"]["commits_between_heads"],
            [output["repository"]["after"]["head"]],
        )
        self.assertFalse(output["repository"]["after"]["dirty"])

    def test_detached_head_is_observed_without_selecting_a_branch(self):
        self.git("checkout", "--quiet", "--detach")

        attempt, completed = self.run_attempt("success")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        repository = json.loads((attempt / "output.json").read_text())["repository"]
        self.assertIsNone(repository["before"]["branch"])
        self.assertIsNone(repository["after"]["branch"])

    def test_invalid_assignment_does_not_create_an_attempt_directory(self):
        assignment_path = self.root / "invalid.json"
        assignment_path.write_text(json.dumps({"schema_version": 1}))
        attempt = self.root / "attempt-invalid"

        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", str(assignment_path), str(attempt)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(attempt.exists())

    def run_attempt(self, scenario, timeout_seconds=5, extra_args=None):
        assignment = {
            "schema_version": 1,
            "objective": f"Exercise {scenario}.",
            "workspace": str(self.workspace),
            "command": [sys.executable, str(FIXTURE), scenario, *(extra_args or [])],
            "timeout_seconds": timeout_seconds,
        }
        assignment_path = self.root / f"{scenario}.json"
        assignment_path.write_text(json.dumps(assignment))
        attempt = self.root / f"attempt-{scenario}"
        completed = subprocess.run(
            [sys.executable, "-m", "afk_attempt", str(assignment_path), str(attempt)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        return attempt, completed

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.workspace, check=True, text=True, capture_output=True
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
