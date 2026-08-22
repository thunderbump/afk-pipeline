import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_plan_accept_contract import planner_input, proposed_plan

ROOT = Path(__file__).parents[1]


class PlanAcceptanceCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "acceptance.json"
        self.result = self.root / "result"
        request = planner_input()
        self.value = {
            "schema_version": 1,
            "planner_input": request,
            "plan": proposed_plan(request),
        }

    def test_seals_an_accepted_plan_record(self):
        completed = self.invoke()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "accepted")
        self.assertEqual(output["source"], {"kind": "bead", "id": "central-example"})
        self.assertEqual(output["acceptance"]["status"], "accepted")
        self.assertEqual(output["acceptance"]["policy"], "contract-valid-proposed-v1")
        self.assertEqual(output["acceptance"]["basis"], "structural_validity_only")
        self.assertEqual(
            output["acceptance"]["plan_sha256"], self.value["plan"]["plan_sha256"]
        )
        self.assertEqual(
            json.loads((self.result / "input.json").read_text()), self.value
        )
        self.assertFalse((self.result / "output.json.tmp").exists())

    def test_needs_human_seals_an_unaccepted_result(self):
        request = planner_input()
        self.value["plan"] = proposed_plan(
            request, ["The target documentation set is not named."]
        )

        completed = self.invoke()

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "unaccepted")
        self.assertEqual(output["decision"], "needs_human")
        self.assertIsNone(output["acceptance"])

    def test_tampered_plan_and_existing_destination_do_not_mutate(self):
        self.value["plan"] = copy.deepcopy(self.value["plan"])
        self.value["plan"]["plan_sha256"] = "0" * 64

        tampered = self.invoke()

        self.assertEqual(tampered.returncode, 2)
        self.assertFalse(self.result.exists())

        request = planner_input()
        self.value["plan"] = proposed_plan(request)
        self.result.mkdir()
        existing = self.invoke()
        self.assertEqual(existing.returncode, 2)
        self.assertEqual(list(self.result.iterdir()), [])

    def test_help_is_available_without_input(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_plan_accept", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("ACCEPTANCE_JSON RESULT_DIRECTORY", completed.stdout)

    def invoke(self):
        self.input_path.write_text(json.dumps(self.value))
        return subprocess.run(
            [sys.executable, "-m", "afk_plan_accept", self.input_path, self.result],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
