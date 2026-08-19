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
FIXTURE = ROOT / "tests" / "fixture_preflight_agent.py"


class PreflightCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "preflight.json"
        self.result = self.root / "result"
        self.input = {
            "schema_version": 1,
            "source": {"kind": "bead", "id": "central-43zn.32"},
            "title": "Expose Coordinator terminal decision through Run Preparer",
            "acceptance_criteria": (
                "The terminal decision is recorded and tested. Validation is shared "
                "through one contract module."
            ),
            "evidence_catalog": [
                {
                    "category": "repository_validation",
                    "route": "repository validation",
                    "can_prove": "Behavior covered by the configured repository command.",
                },
                {
                    "category": "pipeline_evidence",
                    "route": "AFK committed change and Review",
                    "can_prove": "Committed code structure and review findings.",
                },
                {
                    "category": "operator_external",
                    "route": "operator handoff",
                    "can_prove": "Host, deployment, service, and HTTP behavior.",
                },
            ],
            "timeout_seconds": 5,
        }

    def test_repository_and_pipeline_requests_proceed(self):
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "proceed")
        self.assertEqual(output["source"], self.input["source"])
        self.assertEqual(
            [request["category"] for request in output["requests"]],
            ["repository_validation", "pipeline_evidence"],
        )
        self.assertEqual(
            output["classifier"],
            {
                "kind": "inference",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "status": "completed",
            },
        )
        self.assertEqual(
            output["artifacts"], {"events": "events.jsonl", "stderr": "stderr.log"}
        )
        self.assertEqual(
            json.loads((self.result / "input.json").read_text()), self.input
        )
        self.assertTrue((self.result / "events.jsonl").is_file())
        self.assertFalse((self.result / "output.json.tmp").exists())

    def test_operator_owned_requests_pause_before_implementation(self):
        self.input["source"]["id"] = "central-6xx4.1"
        self.input["title"] = "Register Operations WebUI as a first-class Project"
        self.input["acceptance_criteria"] = (
            "Tests, build, deployment and HTTP verification pass."
        )

        completed = self.invoke("pause")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(
            [request["category"] for request in output["requests"]],
            [
                "repository_validation",
                "operator_external",
                "operator_external",
                "operator_external",
            ],
        )
        self.assertEqual(
            [request["request"] for request in output["requests"][1:]],
            ["Build passes.", "Deployment passes.", "HTTP verification passes."],
        )

    def test_invalid_classification_fails_closed_with_a_sealed_pause(self):
        completed = self.invoke("invalid-classification")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["classifier"]["status"], "failed")
        self.assertEqual(output["requests"], [])
        self.assertIn("nonempty array", output["classification_error"])

    def test_invalid_input_and_existing_result_are_refused_without_replacement(self):
        self.input["acceptance_criteria"] = ""
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

        self.input["acceptance_criteria"] = "Commit the result."
        self.result.mkdir()
        sentinel = self.result / "keep.txt"
        sentinel.write_text("caller data\n")
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(self.result.iterdir()), [sentinel])

    def test_agent_protocol_and_launch_failures_seal_a_pause(self):
        completed = self.invoke("invalid-events")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["agent"]["status"], "error")

        self.result = self.root / "launch-failure"
        completed = self.invoke("unused", command=[str(self.root / "missing-agent")])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "pause")
        self.assertIsNone(output["agent"])
        self.assertIn("error", output["process"])

    def test_timeout_and_interrupt_terminate_the_classifier_process_group(self):
        marker = self.root / "timed-out-descendant.pid"
        self.input["timeout_seconds"] = 1
        completed = self.invoke(
            "hang", command=[sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        self.assertEqual(output["decision"], "pause")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

        marker = self.root / "interrupted-descendant.pid"
        self.result = self.root / "interrupted"
        self.input["timeout_seconds"] = 5
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(
            [sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "afk_preflight", self.input_path, self.result],
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
        process.send_signal(signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 1, stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "interrupted")
        self.assertEqual(output["decision"], "pause")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def invoke(self, scenario, command=None):
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(
            command or [sys.executable, str(FIXTURE), scenario]
        )
        return subprocess.run(
            [sys.executable, "-m", "afk_preflight", self.input_path, self.result],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def environment(self, command):
        environment = os.environ.copy()
        environment["AFK_PREFLIGHT_AGENT_COMMAND"] = json.dumps(command)
        return environment


if __name__ == "__main__":
    unittest.main()
