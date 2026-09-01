import copy
import unittest

from afk_plan.contract import build_plan, validate_input, validate_plan
from afk_plan.task import SYSTEM_PROMPT


def capability_input():
    return {
        "schema_version": 2,
        "parent": {
            "id": "central-capability",
            "title": "Route work by automation capability",
            "description": "Keep evidence and execution ownership separate.",
            "acceptance_criteria": (
                "The repository change is implemented. "
                "The deployed route is checked automatically. "
                "A credential owner supplies the unavailable secret."
            ),
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
                        },
                        {
                            "owner": "Caller automation",
                            "executor": "caller_agent",
                            "evidence_route": "external_check",
                            "phases": ["closure"],
                        },
                        {
                            "owner": "Credential owner",
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


def planner_input():
    return capability_input()


def capability_proposal():
    sources = [
        "The repository change is implemented.",
        "The deployed route is checked automatically.",
        "A credential owner supplies the unavailable secret.",
    ]
    statements = [
        "Implement the repository change.",
        "Check the deployed route with caller automation.",
        "Obtain the unavailable credential from its owner.",
    ]
    specs = [
        ("implementation", "AFK Run", "implementation", "afk_run", "pipeline_run"),
        (
            "host-check",
            "Caller automation",
            "closure",
            "caller_agent",
            "external_check",
        ),
        ("credential", "Credential owner", "closure", "outside_help", "external_check"),
    ]
    children = []
    for index, (local_id, owner, phase, executor, evidence) in enumerate(specs, 1):
        child = {
            "local_id": local_id,
            "title": statements[index - 1],
            "objective": statements[index - 1],
            "criteria": [f"criterion-{index}"],
            "project": "afk-pipeline",
            "owner": owner,
            "phase": phase,
            "executor": executor,
            "evidence_route": evidence,
            "depends_on": [] if index == 1 else [specs[index - 2][0]],
        }
        if executor == "outside_help":
            child["outside_help_reason"] = "missing_credentials"
        children.append(child)
    return {
        "schema_version": 2,
        "criteria": [
            {
                "id": f"criterion-{index}",
                "source_text": source,
                "statement": statements[index - 1],
            }
            for index, source in enumerate(sources, 1)
        ],
        "children": children,
        "ambiguities": [],
    }


def proposal():
    return capability_proposal()


class PlanContractTest(unittest.TestCase):
    def test_rejects_retired_routing_v1_input(self):
        request = capability_input()
        request["schema_version"] = 1
        request["catalog"]["schema_version"] = 1
        with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
            validate_input(request)

    def test_prompt_describes_capability_evidence(self):
        prompt = SYSTEM_PROMPT.lower()
        self.assertIn("lacks a required capability", prompt)
        self.assertIn("evidence of the work performed", prompt)
        self.assertNotIn("approval", prompt)
        self.assertNotIn("handoff", prompt)

    def test_builds_and_revalidates_capability_plan(self):
        request = validate_input(capability_input())
        plan = build_plan(request, capability_proposal())
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(
            [child["readiness"] for child in plan["children"]],
            ["ready-for-agent", "ready-for-agent", "ready-for-human"],
        )
        self.assertEqual(validate_plan(request, plan), plan)

    def test_ambiguity_needs_clarification(self):
        candidate = capability_proposal()
        candidate["ambiguities"] = ["The target environment is not named."]
        plan = build_plan(validate_input(capability_input()), candidate)
        self.assertEqual(plan["status"], "needs_clarification")

    def test_outside_help_requires_cataloged_reason_and_external_evidence(self):
        missing = capability_input()
        missing["catalog"]["projects"][0]["routes"][2].pop("outside_help_reason")
        with self.assertRaisesRegex(ValueError, "outside_help_reason"):
            validate_input(missing)

        wrong = capability_input()
        wrong["catalog"]["projects"][0]["routes"][2]["evidence_route"] = (
            "repository_check"
        )
        with self.assertRaisesRegex(ValueError, "external_check"):
            validate_input(wrong)

    def test_rejects_bad_coverage_route_graph_and_digest(self):
        request = validate_input(capability_input())

        missing = capability_proposal()
        missing["criteria"].pop()
        missing["children"].pop()
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            build_plan(request, missing)

        route = capability_proposal()
        route["children"][0]["owner"] = "invented owner"
        with self.assertRaisesRegex(ValueError, "catalog route"):
            build_plan(request, route)

        cycle = capability_proposal()
        cycle["children"][0]["depends_on"] = ["credential"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            build_plan(request, cycle)

        plan = build_plan(request, capability_proposal())
        tampered = copy.deepcopy(plan)
        tampered["plan_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_plan(request, tampered)


if __name__ == "__main__":
    unittest.main()
