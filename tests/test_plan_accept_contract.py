import copy
import unittest

from afk_plan.contract import build_plan, validate_input
from afk_plan_accept.contract import PlanNeedsClarification, accept_plan


def planner_input():
    return validate_input(
        {
            "schema_version": 2,
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
                "schema_version": 2,
                "projects": [
                    {
                        "slug": "example",
                        "routes": [
                            {
                                "owner": "AFK Run",
                                "executor": "afk_run",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation", "closure"],
                            },
                            {
                                "owner": "Caller agent",
                                "executor": "caller_agent",
                                "evidence_route": "external_check",
                                "phases": ["implementation", "closure"],
                            },
                            {
                                "owner": "Credential holder",
                                "executor": "outside_help",
                                "outside_help_reason": "missing_credentials",
                                "evidence_route": "external_check",
                                "phases": ["closure"],
                            },
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
            "schema_version": 2,
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
                    "owner": "AFK Run",
                    "phase": "implementation",
                    "executor": "afk_run",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                },
                {
                    "local_id": "documentation",
                    "title": "Update the documentation",
                    "objective": "Document the current behavior.",
                    "criteria": ["criterion-2"],
                    "project": "example",
                    "owner": "AFK Run",
                    "phase": "closure",
                    "executor": "afk_run",
                    "evidence_route": "pipeline_run",
                    "depends_on": ["implementation"],
                },
            ],
            "ambiguities": ambiguities or [],
        },
    )


def capability_input():
    return planner_input()


def capability_plan(request, executor="caller_agent", ambiguities=None):
    plan = proposed_plan(request, ambiguities)
    for child in plan["children"]:
        child["owner"] = "Caller agent"
        child["executor"] = "caller_agent"
        child["evidence_route"] = "external_check"
    if executor == "outside_help":
        child = plan["children"][-1]
        child["owner"] = "Credential holder"
        child["executor"] = "outside_help"
        child["outside_help_reason"] = "missing_credentials"
    # Rebuild after changing the canonical child fields.
    children = [
        {key: value for key, value in child.items() if key != "readiness"}
        for child in plan["children"]
    ]
    return build_plan(
        request,
        {
            "schema_version": 2,
            "criteria": plan["criteria"],
            "children": children,
            "ambiguities": plan["ambiguities"],
        },
    )


class PlanAcceptanceContractTest(unittest.TestCase):
    def test_accepts_any_unambiguous_contract_valid_plan(self):
        request = planner_input()
        plan = proposed_plan(request)

        accepted = accept_plan(request, plan)

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["policy"], "contract-valid-capability-plan-v2")
        self.assertEqual(accepted["plan"], plan)
        self.assertEqual(len(accepted["acceptance_sha256"]), 64)

    def test_ambiguity_needs_clarification(self):
        request = planner_input()
        plan = proposed_plan(request, ["The target documentation set is not named."])

        with self.assertRaisesRegex(PlanNeedsClarification, "clarification"):
            accept_plan(request, plan)

    def test_tampered_plan_is_rejected(self):
        request = planner_input()
        plan = copy.deepcopy(proposed_plan(request))
        plan["children"][0]["owner"] = "Invented owner"

        with self.assertRaises(ValueError):
            accept_plan(request, plan)


if __name__ == "__main__":
    unittest.main()
