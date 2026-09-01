import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.inference_cli_fixture import install_pi

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_plan_agent.py"


class PlanCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "planner.json"
        self.result = self.root / "result"
        self.request = {
            "schema_version": 2,
            "parent": {
                "id": "central-43zn.33.1",
                "title": "Build Acceptance Planner",
                "description": "Create the planner component.",
                "acceptance_criteria": "The change is implemented and tested.",
                "labels": ["project:afk-pipeline"],
            },
            "catalog": {
                "schema_version": 2,
                "projects": [
                    {
                        "slug": "afk-pipeline",
                        "routes": [
                            {
                                "owner": "AFK Run",
                                "executor": "afk_run",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation"],
                            }
                        ],
                    }
                ],
            },
            "timeout_seconds": 5,
        }

    def test_seals_capability_oriented_direct_routing(self):
        completed = self.invoke("capability-direct")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["schema_version"], 2)
        self.assertEqual(output["routing"]["routes"][0]["executor"], "afk_run")
        self.assertNotIn("execution", output["routing"]["routes"][0])
        self.assertIsNone(output["plan"])
        receipt = json.loads((self.result / "inference/receipt.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "NO_TOOLS")

    def test_rejects_retired_routing_v1_before_result_creation(self):
        self.request["schema_version"] = 1
        self.request["catalog"]["schema_version"] = 1
        completed = self.invoke("capability-direct")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("schema_version must be 2", completed.stderr)
        self.assertFalse(self.result.exists())

    def test_invalid_proposal_seals_failed_output(self):
        completed = self.invoke("invalid")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["routing"])
        self.assertIsNone(output["plan"])

    def test_help_is_available_without_input(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_plan", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)

    def invoke(self, scenario):
        self.input_path.write_text(json.dumps(self.request))
        bin_directory = self.root / "bin"
        bin_directory.mkdir(exist_ok=True)
        install_pi(bin_directory, FIXTURE, scenario)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        return subprocess.run(
            [sys.executable, "-m", "afk_plan", self.input_path, self.result],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
