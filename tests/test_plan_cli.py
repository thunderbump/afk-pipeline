import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
            "schema_version": 1,
            "parent": {
                "id": "central-43zn.33.1",
                "title": "Build Acceptance Planner",
                "description": "Create the first planner component.",
                "acceptance_criteria": "The change is implemented and tested.",
                "labels": ["project:afk-pipeline"],
            },
            "catalog": {
                "schema_version": 1,
                "projects": [
                    {
                        "slug": "afk-pipeline",
                        "routes": [
                            {
                                "owner": "AFK implementation agent",
                                "execution": "agent",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation"],
                            }
                        ],
                    }
                ],
            },
            "timeout_seconds": 5,
        }

    def test_seals_a_valid_unapproved_plan_and_raw_evidence(self):
        completed = self.invoke("valid")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["source"], {"kind": "bead", "id": "central-43zn.33.1"})
        self.assertEqual(output["planner"]["model"], "gpt-5.6-luna")
        self.assertEqual(output["plan"]["status"], "proposed")
        self.assertIsNone(output["plan"]["authorization"])
        self.assertEqual(output["plan"]["children"][0]["readiness"], "ready-for-agent")
        self.assertEqual(
            output["artifacts"], {"events": "events.jsonl", "stderr": "stderr.log"}
        )
        self.assertEqual(
            json.loads((self.result / "input.json").read_text()), self.request
        )
        self.assertTrue((self.result / "events.jsonl").stat().st_size > 0)
        self.assertFalse((self.result / "output.json.tmp").exists())

    def test_invalid_proposal_seals_failed_output_without_a_plan(self):
        completed = self.invoke("invalid-proposal")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["error_category"], "invalid_proposal")
        self.assertIsNone(output["plan"])

    def test_process_failure_cannot_publish_a_plan(self):
        completed = self.invoke("process-failure")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["error_category"], "agent_process")
        self.assertIsNone(output["plan"])
        self.assertEqual(output["process"]["exit_code"], 7)

    def test_invalid_events_seal_an_agent_protocol_failure(self):
        completed = self.invoke("invalid-events")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["error_category"], "agent_protocol")
        self.assertIsNone(output["plan"])

    def test_invalid_input_and_existing_result_exit_two_without_mutation(self):
        self.request["parent"]["acceptance_criteria"] = ""
        invalid = self.invoke("valid")
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse(self.result.exists())

        self.request["parent"]["acceptance_criteria"] = (
            "The change is implemented and tested."
        )
        self.result.mkdir()
        existing = self.invoke("valid")
        self.assertEqual(existing.returncode, 2)
        self.assertEqual(list(self.result.iterdir()), [])

    def test_help_is_available_without_input(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_plan", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("PLANNER_JSON RESULT_DIRECTORY", completed.stdout)

    def invoke(self, scenario):
        self.input_path.write_text(json.dumps(self.request))
        environment = os.environ.copy()
        environment["AFK_PLAN_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(FIXTURE), scenario]
        )
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
