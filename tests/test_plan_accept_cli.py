import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk_plan.contract import build_routing
from tests.test_plan_accept_contract import capability_input, capability_plan

ROOT = Path(__file__).parents[1]


class PlanAcceptCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "acceptance.json"
        self.result = self.root / "result"
        request = capability_input()
        self.value = {
            "schema_version": 2,
            "planner_input": request,
            "plan": capability_plan(request),
        }

    def test_accepts_capability_plan(self):
        completed = self.invoke()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "accepted")
        self.assertEqual(output["policy"], "contract-valid-capability-plan-v2")

    def test_direct_afk_run_is_accepted(self):
        request = capability_input()
        plan = capability_plan(request)
        routing, _ = build_routing(
            request,
            {
                "schema_version": 2,
                "decision": "direct",
                "criteria": plan["criteria"],
                "direct_routes": [
                    {
                        "criterion": f"criterion-{index}",
                        "project": "example",
                        "owner": "AFK Run",
                        "phase": "implementation",
                        "executor": "afk_run",
                        "evidence_route": "pipeline_run",
                    }
                    for index in (1, 2)
                ],
                "children": [],
                "ambiguities": [],
            },
        )
        self.value = {"schema_version": 2, "planner_input": request, "routing": routing}
        completed = self.invoke()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "direct")
        self.assertEqual(output["policy"], "pipeline-compatible-capability-direct-v2")

    def test_ambiguity_needs_clarification(self):
        request = capability_input()
        self.value["plan"] = capability_plan(request, ambiguities=["Unclear target."])
        completed = self.invoke()
        self.assertEqual(completed.returncode, 1)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "needs_clarification")

    def test_rejects_retired_routing_v1(self):
        self.value["schema_version"] = 1
        completed = self.invoke()
        self.assertEqual(completed.returncode, 2)
        self.assertIn("schema_version must be 2", completed.stderr)
        self.assertFalse(self.result.exists())

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
