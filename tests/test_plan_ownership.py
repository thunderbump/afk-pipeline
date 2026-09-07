"""Ownership regression from the retained central-shfv.3 routing failure."""

import copy
import json
import unittest
from pathlib import Path

from afk_plan.contract import build_routing, validate_input
from afk_plan.task import build_task
from afk_plan_accept.contract import accept_direct, accept_plan

FIXTURE = Path(__file__).parent / "fixtures" / "wrong-project-plan.json"


class PlanOwnershipTest(unittest.TestCase):
    def setUp(self):
        retained = json.loads(FIXTURE.read_text())
        self.request = validate_input(retained["request"])
        self.plan = retained["plan"]

    def test_retained_wrong_owner_plan_is_rejected_by_policy(self):
        self.assertIn("project:operations-webui", self.request["parent"]["labels"])
        self.assertEqual({c["project"] for c in self.plan["children"]}, {"bump-eqemu"})
        with self.assertRaisesRegex(ValueError, "project_justification"):
            accept_plan(self.request, self.plan)

    def test_task_exposes_label_owned_source_project_without_mutating_request(self):
        original = copy.deepcopy(self.request)
        task = build_task(self.request)
        self.assertEqual(task.untrusted_data["source_project"], "operations-webui")
        self.assertEqual(task.untrusted_data["parent"], original["parent"])
        self.assertEqual(self.request, original)

    def cross_project_proposal(self):
        self.request["parent"]["description"] = (
            "Change the renderer in operations-webui and update the producer in bump-eqemu."
        )
        self.request["parent"]["acceptance_criteria"] = (
            "Update the producer in bump-eqemu."
        )
        child = copy.deepcopy(self.plan["children"][0])
        child.pop("readiness")
        child["criteria"] = ["criterion-1"]
        child["project_justification"] = {
            "source_field": "acceptance_criteria",
            "source_text": "Update the producer in bump-eqemu.",
            "rationale": "The parent explicitly requires changing the producer owned by bump-eqemu.",
        }
        return {
            "schema_version": 2,
            "decision": "decompose",
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": self.request["parent"]["acceptance_criteria"],
                    "statement": "Update the producer.",
                }
            ],
            "direct_routes": [],
            "children": [child],
            "ambiguities": [],
        }

    def test_explicit_cross_project_work_retains_grounding_through_policy(self):
        proposal = self.cross_project_proposal()
        routing, plan = build_routing(self.request, proposal)
        accepted = accept_plan(self.request, plan)
        self.assertEqual(routing["routes"][0]["project"], "bump-eqemu")
        self.assertEqual(
            accepted["plan"]["children"][0]["project_justification"],
            proposal["children"][0]["project_justification"],
        )
        self.assertEqual(accepted["basis"], "structural_validity_only")

    def test_cross_project_grounding_cannot_be_missing_invented_or_catalog_only(self):
        proposal = self.cross_project_proposal()
        for key, value in [
            ("source_text", "Invented work in bump-eqemu."),
            ("source_field", "catalog"),
            ("rationale", ""),
            ("source_text", "Update the producer"),
        ]:
            bad = copy.deepcopy(proposal)
            bad["children"][0]["project_justification"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                build_routing(self.request, bad)
        bad = copy.deepcopy(proposal)
        bad["children"][0].pop("project_justification")
        with self.assertRaisesRegex(ValueError, "project_justification"):
            build_routing(self.request, bad)

    def test_repository_local_work_with_example_project_can_remain_direct(self):
        # Keep the real confusing parent text, but model the repository-only slice.
        self.request["parent"]["acceptance_criteria"] = self.plan["criteria"][0][
            "source_text"
        ]
        proposal = {
            "schema_version": 2,
            "decision": "direct",
            "criteria": [self.plan["criteria"][0]],
            "direct_routes": [
                {
                    "criterion": "criterion-1",
                    "project": "operations-webui",
                    "owner": "AFK Run",
                    "phase": "implementation",
                    "executor": "afk_run",
                    "evidence_route": "repository_check",
                }
            ],
            "children": [],
            "ambiguities": [],
        }
        routing, plan = build_routing(self.request, proposal)
        self.assertIsNone(plan)
        self.assertEqual(accept_direct(self.request, routing)["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
