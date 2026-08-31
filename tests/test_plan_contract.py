import copy
import unittest

from afk_plan.contract import build_plan, validate_input, validate_plan
from afk_plan.task import CAPABILITY_SYSTEM_PROMPT


def planner_input():
    return {
        "schema_version": 1,
        "parent": {
            "id": "central-43zn.45",
            "title": "Refresh docs and verify the live site",
            "description": "Update the repository-owned docs.",
            "acceptance_criteria": (
                "The AFK docs describe the current interface. "
                "The Operations pages are updated and the live route returns 200."
            ),
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
                },
                {
                    "slug": "operations-webui",
                    "routes": [
                        {
                            "owner": "Operations documentation agent",
                            "execution": "agent",
                            "evidence_route": "pipeline_run",
                            "phases": ["closure"],
                        },
                        {
                            "owner": "Operations owner",
                            "execution": "external",
                            "evidence_route": "external_check",
                            "phases": ["closure"],
                        },
                    ],
                },
            ],
        },
        "timeout_seconds": 30,
    }


def proposal():
    return {
        "schema_version": 1,
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The AFK docs describe the current interface.",
                "statement": "Refresh the AFK interface documentation.",
            },
            {
                "id": "criterion-2",
                "source_text": "The Operations pages are updated",
                "statement": "Update the Operations pages.",
            },
            {
                "id": "criterion-3",
                "source_text": "and the live route returns 200.",
                "statement": "Verify the deployed route.",
            },
        ],
        "children": [
            {
                "local_id": "implementation",
                "title": "Refresh AFK documentation",
                "objective": "Make the AFK repository documentation current.",
                "criteria": ["criterion-1"],
                "project": "afk-pipeline",
                "owner": "AFK implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": [],
            },
            {
                "local_id": "operations-closure",
                "title": "Publish Operations documentation",
                "objective": "Update the Operations pages from pushed authority.",
                "criteria": ["criterion-2"],
                "project": "operations-webui",
                "owner": "Operations documentation agent",
                "phase": "closure",
                "execution": "agent",
                "evidence_route": "pipeline_run",
                "depends_on": ["implementation"],
            },
            {
                "local_id": "live-verification",
                "title": "Verify the live route",
                "objective": "Confirm the deployed route returns HTTP 200.",
                "criteria": ["criterion-3"],
                "project": "operations-webui",
                "owner": "Operations owner",
                "phase": "closure",
                "execution": "external",
                "evidence_route": "external_check",
                "depends_on": ["operations-closure"],
                "handoff": {
                    "authority": "Operations owner",
                    "subject_fields": ["environment"],
                    "completion_record": "external_check",
                },
            },
        ],
        "ambiguities": [],
    }


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
    route_specs = [
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
    for index, (local_id, owner, phase, executor, evidence) in enumerate(
        route_specs, start=1
    ):
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
            "depends_on": [] if index == 1 else [route_specs[index - 2][0]],
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
            for index, source in enumerate(sources, start=1)
        ],
        "children": children,
        "ambiguities": [],
    }


class PlanContractTest(unittest.TestCase):
    def test_capability_prompt_describes_only_unavailable_capability_evidence(self):
        prompt = CAPABILITY_SYSTEM_PROMPT.lower()
        self.assertIn("lacks a required capability", prompt)
        self.assertIn("evidence of the work performed", prompt)
        self.assertIn("external_check", prompt)
        self.assertNotIn("approval", prompt)
        self.assertNotIn("waiver", prompt)

    def test_builds_capability_routing_without_approval_semantics(self):
        request = validate_input(capability_input())

        plan = build_plan(request, capability_proposal())

        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(
            [child["readiness"] for child in plan["children"]],
            ["ready-for-agent", "ready-for-agent", "ready-for-human"],
        )
        self.assertEqual(
            plan["children"][2]["outside_help_reason"], "missing_credentials"
        )
        self.assertEqual(validate_plan(request, plan), plan)

    def test_capability_ambiguity_needs_clarification(self):
        candidate = capability_proposal()
        candidate["ambiguities"] = ["The target environment is not named."]

        plan = build_plan(validate_input(capability_input()), candidate)

        self.assertEqual(plan["status"], "needs_clarification")

    def test_outside_help_requires_a_cataloged_reason(self):
        missing = capability_input()
        missing["catalog"]["projects"][0]["routes"][2].pop("outside_help_reason")
        with self.assertRaisesRegex(ValueError, "outside_help_reason"):
            validate_input(missing)

        extra = capability_input()
        extra["catalog"]["projects"][0]["routes"][0]["outside_help_reason"] = (
            "human_judgment"
        )
        with self.assertRaisesRegex(ValueError, "requires outside_help"):
            validate_input(extra)

    def test_outside_help_requires_external_check_evidence(self):
        request = capability_input()
        request["catalog"]["projects"][0]["routes"][2]["evidence_route"] = (
            "repository_check"
        )
        with self.assertRaisesRegex(
            ValueError, "outside_help evidence_route must be external_check"
        ):
            validate_input(request)

        request = capability_input()
        proposal = capability_proposal()
        proposal["children"][2]["evidence_route"] = "repository_check"
        with self.assertRaisesRegex(
            ValueError, "outside_help evidence_route must be external_check"
        ):
            build_plan(validate_input(request), proposal)

    def test_rejects_retired_approval_routes_and_v1_human_handoffs(self):
        retired_route = planner_input()
        retired_route["catalog"]["projects"][1]["routes"][1]["evidence_route"] = (
            "human_attestation"
        )
        with self.assertRaisesRegex(ValueError, "evidence_route is invalid"):
            validate_input(retired_route)

        human_handoff = planner_input()
        human_handoff["catalog"]["projects"][1]["routes"][1]["execution"] = "human"
        with self.assertRaisesRegex(ValueError, "execution is invalid"):
            validate_input(human_handoff)

    def test_builds_a_canonical_unapproved_plan(self):
        accepted_input = validate_input(planner_input())

        plan = build_plan(accepted_input, proposal())

        self.assertEqual(plan["status"], "proposed")
        self.assertIsNone(plan["authorization"])
        self.assertEqual(
            [child["readiness"] for child in plan["children"]],
            ["ready-for-agent", "ready-for-agent", "ready-for-human"],
        )
        self.assertEqual(len(plan["parent"]["sha256"]), 64)
        self.assertEqual(len(plan["catalog_sha256"]), 64)
        self.assertEqual(len(plan["plan_sha256"]), 64)
        self.assertEqual(validate_plan(accepted_input, plan), plan)

    def test_ambiguity_requires_human_without_authorizing_publication(self):
        candidate = proposal()
        candidate["ambiguities"] = ["The deployment environment is not named."]

        plan = build_plan(validate_input(planner_input()), candidate)

        self.assertEqual(plan["status"], "needs_human")
        self.assertIsNone(plan["authorization"])

    def test_external_host_closure_uses_the_real_project_and_handoff(self):
        request = {
            "schema_version": 1,
            "parent": {
                "id": "central-43zn.33",
                "title": "Keep host-only checks outside repair",
                "description": "Separate implementation from host-owned closure.",
                "acceptance_criteria": (
                    "Implementation review cannot create host deployment work. "
                    "The configured host check is completed by its operator."
                ),
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
                            },
                            {
                                "owner": "EQEmu test host operator",
                                "execution": "external",
                                "evidence_route": "external_check",
                                "phases": ["closure"],
                            },
                        ],
                    }
                ],
            },
            "timeout_seconds": 30,
        }
        candidate = {
            "schema_version": 1,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": (
                        "Implementation review cannot create host deployment work."
                    ),
                    "statement": "Keep host deployment outside implementation repair.",
                },
                {
                    "id": "criterion-2",
                    "source_text": (
                        "The configured host check is completed by its operator."
                    ),
                    "statement": "Complete the configured external host check.",
                },
            ],
            "children": [
                {
                    "local_id": "implementation-boundary",
                    "title": "Enforce the implementation boundary",
                    "objective": "Prevent Review from creating host deployment work.",
                    "criteria": ["criterion-1"],
                    "project": "afk-pipeline",
                    "owner": "AFK implementation agent",
                    "phase": "implementation",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                },
                {
                    "local_id": "host-closure",
                    "title": "Run the host-owned closure check",
                    "objective": "Verify the named commit on the named environment.",
                    "criteria": ["criterion-2"],
                    "project": "afk-pipeline",
                    "owner": "EQEmu test host operator",
                    "phase": "closure",
                    "execution": "external",
                    "evidence_route": "external_check",
                    "depends_on": ["implementation-boundary"],
                    "handoff": {
                        "authority": "EQEmu test host operator",
                        "subject_fields": ["commit", "environment"],
                        "completion_record": "external_check",
                    },
                },
            ],
            "ambiguities": [],
        }

        plan = build_plan(validate_input(request), candidate)

        self.assertEqual(plan["children"][1]["project"], "afk-pipeline")
        self.assertEqual(plan["children"][1]["readiness"], "ready-for-human")

    def test_rejects_omitted_source_text(self):
        candidate = proposal()
        candidate["criteria"].pop()
        candidate["children"].pop()

        with self.assertRaisesRegex(ValueError, "exactly cover"):
            build_plan(validate_input(planner_input()), candidate)

    def test_rejects_duplicate_criterion_ownership(self):
        candidate = proposal()
        candidate["children"][1]["criteria"].append("criterion-1")

        with self.assertRaisesRegex(ValueError, "exactly once"):
            build_plan(validate_input(planner_input()), candidate)

    def test_rejects_a_route_not_in_the_catalog(self):
        candidate = proposal()
        candidate["children"][0]["evidence_route"] = "repository_check"

        with self.assertRaisesRegex(ValueError, "catalog route"):
            build_plan(validate_input(planner_input()), candidate)

    def test_rejects_cycles_and_closure_before_implementation(self):
        cyclic = proposal()
        cyclic["children"][0]["depends_on"] = ["live-verification"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            build_plan(validate_input(planner_input()), cyclic)

        early_closure = proposal()
        early_closure["children"][1]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "follow implementation"):
            build_plan(validate_input(planner_input()), early_closure)

        reversed_phase = proposal()
        reversed_phase["children"][0]["depends_on"] = ["operations-closure"]
        reversed_phase["children"][1]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "must not follow closure"):
            build_plan(validate_input(planner_input()), reversed_phase)

    def test_rejects_an_untrusted_owner_or_handoff_authority(self):
        untrusted = proposal()
        untrusted["children"][0]["owner"] = "invented owner"
        with self.assertRaisesRegex(ValueError, "catalog route"):
            build_plan(validate_input(planner_input()), untrusted)

        mismatched = proposal()
        mismatched["children"][2]["handoff"]["authority"] = "Another operator"
        with self.assertRaisesRegex(ValueError, "trusted child owner"):
            build_plan(validate_input(planner_input()), mismatched)

    def test_rejects_tampered_digests_and_readiness(self):
        accepted_input = validate_input(planner_input())
        plan = build_plan(accepted_input, proposal())

        for field, replacement in (
            ("catalog_sha256", "0" * 64),
            ("plan_sha256", "0" * 64),
        ):
            changed = copy.deepcopy(plan)
            changed[field] = replacement
            with self.assertRaises(ValueError):
                validate_plan(accepted_input, changed)

        changed = copy.deepcopy(plan)
        changed["children"][0]["readiness"] = "ready-for-human"
        with self.assertRaisesRegex(ValueError, "readiness"):
            validate_plan(accepted_input, changed)

    def test_parent_must_have_one_cataloged_project_label(self):
        missing = planner_input()
        missing["parent"]["labels"] = ["ready-for-human"]
        with self.assertRaisesRegex(ValueError, "project label"):
            validate_input(missing)

        unknown = planner_input()
        unknown["parent"]["labels"] = ["project:unknown"]
        with self.assertRaisesRegex(ValueError, "catalog"):
            validate_input(unknown)


if __name__ == "__main__":
    unittest.main()
