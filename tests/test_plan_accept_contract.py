import copy
import unittest

from afk_plan.contract import build_plan, validate_input
from afk_plan_accept.contract import PlanNeedsHuman, accept_plan


def planner_input():
    return validate_input(
        {
            "schema_version": 1,
            "parent": {
                "id": "central-example",
                "title": "Implement and document one change",
                "description": "One unambiguous parent task.",
                "acceptance_criteria": (
                    "The change is implemented. The current documentation is updated."
                ),
                "labels": ["project:example"],
            },
            "catalog": {
                "schema_version": 1,
                "projects": [
                    {
                        "slug": "example",
                        "routes": [
                            {
                                "owner": "Example agent",
                                "execution": "agent",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation", "closure"],
                            }
                        ],
                    }
                ],
            },
            "timeout_seconds": 30,
        }
    )


def proposed_plan(request, ambiguities=None):
    return build_plan(
        request,
        {
            "schema_version": 1,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": "The change is implemented.",
                    "statement": "Implement the change.",
                },
                {
                    "id": "criterion-2",
                    "source_text": "The current documentation is updated.",
                    "statement": "Update the current documentation.",
                },
            ],
            "children": [
                {
                    "local_id": "implementation",
                    "title": "Implement the change",
                    "objective": "Implement the requested behavior.",
                    "criteria": ["criterion-1"],
                    "project": "example",
                    "owner": "Example agent",
                    "phase": "implementation",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                },
                {
                    "local_id": "documentation",
                    "title": "Update the documentation",
                    "objective": "Document the current behavior.",
                    "criteria": ["criterion-2"],
                    "project": "example",
                    "owner": "Example agent",
                    "phase": "closure",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                    "depends_on": ["implementation"],
                },
            ],
            "ambiguities": ambiguities or [],
        },
    )


class PlanAcceptanceContractTest(unittest.TestCase):
    def test_accepts_any_unambiguous_contract_valid_plan(self):
        request = planner_input()
        plan = proposed_plan(request)

        accepted = accept_plan(request, plan)

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["policy"], "contract-valid-proposed-v1")
        self.assertEqual(accepted["basis"], "structural_validity_only")
        self.assertEqual(accepted["parent"], plan["parent"])
        self.assertEqual(accepted["catalog_sha256"], plan["catalog_sha256"])
        self.assertEqual(accepted["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(accepted["plan"], plan)
        self.assertEqual(len(accepted["acceptance_sha256"]), 64)

    def test_needs_human_is_not_automatically_accepted(self):
        request = planner_input()
        plan = proposed_plan(request, ["The target documentation set is not named."])

        with self.assertRaisesRegex(PlanNeedsHuman, "needs human"):
            accept_plan(request, plan)

    def test_tampered_plan_is_rejected_by_the_existing_plan_contract(self):
        request = planner_input()
        plan = copy.deepcopy(proposed_plan(request))
        plan["children"][0]["owner"] = "Invented owner"

        with self.assertRaises(ValueError):
            accept_plan(request, plan)


if __name__ == "__main__":
    unittest.main()
