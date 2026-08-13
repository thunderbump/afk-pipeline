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
FIXTURE = ROOT / "tests" / "fixture_validation.py"


class ValidationCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("fixture repository\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Initial state")

    def test_successful_command_seals_validation_for_unchanged_head(self):
        validation = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "command": [sys.executable, "-c", "print('validation passed')"],
            "timeout_seconds": 5,
        }

        result, completed = self.run_validation(validation)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads((result / "input.json").read_text()), validation)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "passed")
        self.assertEqual(output["process"], {"exit_code": 0, "signal": None})
        self.assertEqual(output["repository"]["before"], output["repository"]["after"])
        self.assertFalse(output["repository"]["head_changed"])
        self.assertEqual((result / "stdout.log").read_text(), "validation passed\n")
        self.assertEqual((result / "stderr.log").read_text(), "")
        self.assertFalse((result / "output.json.tmp").exists())
        self.assertGreaterEqual(output["duration_seconds"], 0)

    def test_nonzero_command_is_a_sealed_failure(self):
        validation = self.validation(
            [
                sys.executable,
                "-c",
                "import sys; print('validation failed', file=sys.stderr); sys.exit(7)",
            ]
        )

        result, completed = self.run_validation(validation)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["process"], {"exit_code": 7, "signal": None})
        self.assertFalse(output["repository"]["head_changed"])
        self.assertEqual((result / "stderr.log").read_text(), "validation failed\n")

    def test_command_that_moves_head_is_a_sealed_failure(self):
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import subprocess; "
                "Path('changed.txt').write_text('changed\\n'); "
                "subprocess.run(['git', 'add', 'changed.txt'], check=True); "
                "subprocess.run(['git', 'commit', '--quiet', '-m', 'Moved HEAD'], check=True)"
            ),
        ]

        result, completed = self.run_validation(self.validation(command))

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["process"], {"exit_code": 0, "signal": None})
        self.assertTrue(output["repository"]["head_changed"])
        self.assertNotEqual(
            output["repository"]["before"]["head"],
            output["repository"]["after"]["head"],
        )

    def test_timeout_is_a_sealed_non_success(self):
        marker = self.root / "timeout-processes.json"
        result, completed = self.run_validation(
            self.validation(
                [sys.executable, str(FIXTURE), "hang", str(marker)],
                timeout_seconds=1,
            )
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        self.assertLess(output["duration_seconds"], 1.8)
        descendant = json.loads(marker.read_text())["descendant"]
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_command_launch_failure_is_sealed(self):
        result, completed = self.run_validation(
            self.validation([str(self.root / "missing-validator")])
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["process"]["exit_code"])
        self.assertIn("missing-validator", output["process"]["error"])

    def test_post_command_git_observation_failure_is_sealed(self):
        result, completed = self.run_validation(
            self.validation(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('.git').rename('.git-damaged')",
                ]
            )
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIsNone(output["repository"]["head_changed"])
        self.assertIn("observation_error", output["repository"])

    def test_detached_head_is_a_valid_implicit_ref(self):
        self.git("checkout", "--quiet", "--detach")

        result, completed = self.run_validation(
            self.validation([sys.executable, "-c", "pass"])
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "passed")
        self.assertIsNone(output["repository"]["before"]["branch"])
        self.assertIsNone(output["repository"]["after"]["branch"])

    def test_interrupt_terminates_command_group_and_seals_result(self):
        marker = self.root / "processes.json"
        validation = self.validation(
            [sys.executable, str(FIXTURE), "hang", str(marker)],
            timeout_seconds=30,
        )
        input_path = self.root / "validation.json"
        input_path.write_text(json.dumps(validation))
        result = self.root / "result"
        validator = subprocess.Popen(
            [sys.executable, "-m", "afk_validate", str(input_path), str(result)],
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
        processes = json.loads(marker.read_text())

        try:
            validator.send_signal(signal.SIGINT)
            stdout, stderr = validator.communicate(timeout=5)
        finally:
            try:
                os.killpg(processes["process"], signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.assertEqual(validator.returncode, 1, stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "interrupted")
        with self.assertRaises(ProcessLookupError):
            os.kill(processes["descendant"], 0)

    def test_invalid_input_does_not_create_result_directory(self):
        validation = self.validation([sys.executable, "-c", "pass"])
        validation["timeout_seconds"] = 0

        result, completed = self.run_validation(validation)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("positive integer", completed.stderr)

    def test_existing_result_directory_is_refused_without_changes(self):
        result = self.root / "result"
        result.mkdir()
        sentinel = result / "keep.txt"
        sentinel.write_text("caller data\n")

        returned_result, completed = self.run_validation(
            self.validation([sys.executable, "-c", "pass"])
        )

        self.assertEqual(returned_result, result)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual([sentinel], list(result.iterdir()))

    def validation(self, command, timeout_seconds=5):
        return {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "command": command,
            "timeout_seconds": timeout_seconds,
        }

    def run_validation(self, validation):
        input_path = self.root / "validation.json"
        input_path.write_text(json.dumps(validation))
        result = self.root / "result"
        completed = subprocess.run(
            [sys.executable, "-m", "afk_validate", str(input_path), str(result)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        return result, completed

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.workspace,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
