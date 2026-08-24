import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk_plan.contract import build_routing
from afk_plan_accept.contract import validate_accepted_output
from tests.test_plan_accept_contract import (
    capability_input,
    capability_plan,
    planner_input,
    proposed_plan,
)

ROOT = Path(__file__).parents[1]


def pipeline_direct_routing(request):
    routing, plan = build_routing(
        request,
        {
            "schema_version": 1,
            "decision": "direct",
            "criteria": proposed_plan(request)["criteria"],
            "direct_routes": [
                {
                    "criterion": f"criterion-{index}",
                    "project": "example",
                    "owner": "Example agent",
                    "phase": "implementation",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                }
                for index in (1, 2)
            ],
            "children": [],
            "ambiguities": [],
        },
    )
    if plan is not None:
        raise AssertionError("direct fixture unexpectedly produced a Plan")
    return routing


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

    def test_accepts_pipeline_compatible_direct_routing_without_a_plan(self):
        request = planner_input()
        routing = pipeline_direct_routing(request)
        self.value = {
            "schema_version": 1,
            "planner_input": request,
            "routing": routing,
        }

        completed = self.invoke()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "direct")
        self.assertEqual(output["source"], {"kind": "bead", "id": "central-example"})
        self.assertEqual(output["policy"], "pipeline-compatible-direct-v1")
        self.assertEqual(
            output["acceptance"]["routing_sha256"], routing["routing_sha256"]
        )
        self.assertEqual(output["acceptance"]["routing"], routing)
        self.assertNotIn("plan", output["acceptance"])
        self.assertEqual(validate_accepted_output(request, output), output)

        tampered = copy.deepcopy(output)
        tampered["acceptance"]["routing"]["routing_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_accepted_output(request, tampered)

    def test_tampered_direct_routing_creates_no_policy_result(self):
        request = planner_input()
        routing = copy.deepcopy(pipeline_direct_routing(request))
        routing["routing_sha256"] = "0" * 64
        self.value = {
            "schema_version": 1,
            "planner_input": request,
            "routing": routing,
        }

        completed = self.invoke()

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

    def test_rejects_incompatible_or_uncertain_direct_routing(self):
        cases = (
            (
                "closure",
                "example",
                "Example agent",
                "agent",
                "pipeline_run",
                "closure",
                [],
            ),
            (
                "human",
                "example",
                "Brian",
                "human",
                "human_attestation",
                "implementation",
                [],
            ),
            (
                "external",
                "example",
                "Host operator",
                "external",
                "external_check",
                "implementation",
                [],
            ),
            (
                "cross-project",
                "other",
                "Other agent",
                "agent",
                "pipeline_run",
                "implementation",
                [],
            ),
            (
                "ambiguous",
                "example",
                "Example agent",
                "agent",
                "pipeline_run",
                "implementation",
                ["The evidence owner is unclear."],
            ),
        )
        for name, project, owner, execution, evidence, phase, ambiguities in cases:
            with self.subTest(name=name):
                request = planner_input()
                request["catalog"]["projects"][0]["routes"].extend(
                    [
                        {
                            "owner": "Brian",
                            "execution": "human",
                            "evidence_route": "human_attestation",
                            "phases": ["implementation"],
                        },
                        {
                            "owner": "Host operator",
                            "execution": "external",
                            "evidence_route": "external_check",
                            "phases": ["implementation"],
                        },
                    ]
                )
                request["catalog"]["projects"].append(
                    {
                        "slug": "other",
                        "routes": [
                            {
                                "owner": "Other agent",
                                "execution": "agent",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation"],
                            }
                        ],
                    }
                )
                direct_routes = [
                    {
                        "criterion": f"criterion-{index}",
                        "project": project,
                        "owner": owner,
                        "phase": phase,
                        "execution": execution,
                        "evidence_route": evidence,
                    }
                    for index in (1, 2)
                ]
                routing, _ = build_routing(
                    request,
                    {
                        "schema_version": 1,
                        "decision": "direct",
                        "criteria": proposed_plan(request)["criteria"],
                        "direct_routes": direct_routes,
                        "children": [],
                        "ambiguities": ambiguities,
                    },
                )
                self.value = {
                    "schema_version": 1,
                    "planner_input": request,
                    "routing": routing,
                }
                self.result = self.root / f"result-{name}"

                completed = self.invoke()

                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((self.result / "output.json").read_text())
                self.assertEqual(output["decision"], "needs_human")
                self.assertEqual(output["error_category"], "direct_incompatible")
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

    def test_v2_direct_afk_run_is_accepted(self):
        request = capability_input()
        routing, plan = build_routing(
            request,
            {
                "schema_version": 2,
                "decision": "direct",
                "criteria": capability_plan(request)["criteria"],
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
        self.assertIsNone(plan)
        self.value = {"schema_version": 2, "planner_input": request, "routing": routing}

        completed = self.invoke()

        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output["decision"], "direct")
        self.assertEqual(output["policy"], "pipeline-compatible-capability-direct-v2")

    def test_v2_ambiguity_needs_clarification(self):
        request = capability_input()
        self.value = {
            "schema_version": 2,
            "planner_input": request,
            "plan": capability_plan(request, ambiguities=["The target is unclear."]),
        }

        completed = self.invoke()

        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(output["decision"], "needs_clarification")
        self.assertNotIn("approval", json.dumps(output))

    def test_v2_direct_outside_help_preserves_the_reason(self):
        request = capability_input()
        routing, _ = build_routing(
            request,
            {
                "schema_version": 2,
                "decision": "direct",
                "criteria": capability_plan(request)["criteria"],
                "direct_routes": [
                    {
                        "criterion": f"criterion-{index}",
                        "project": "example",
                        "owner": "Credential holder",
                        "phase": "closure",
                        "executor": "outside_help",
                        "outside_help_reason": "missing_credentials",
                        "evidence_route": "human_attestation",
                    }
                    for index in (1, 2)
                ],
                "children": [],
                "ambiguities": [],
            },
        )
        self.value = {"schema_version": 2, "planner_input": request, "routing": routing}

        completed = self.invoke()

        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(output["decision"], "outside_help")
        self.assertEqual(output["error_category"], "missing_credentials")
        self.assertNotIn("approval", json.dumps(output))

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
