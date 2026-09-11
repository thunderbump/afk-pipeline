import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from afk_assess.contract import validate_assessment
from afk_assess.task import ASSESSMENT_INSTRUCTIONS
from afk_assess.task import build_task as build_assessment_task
from afk_attempt.task import ATTEMPT_INSTRUCTIONS
from afk_attempt.task import build_task as build_attempt_task
from afk_inference import Capability, ResponseRejected
from afk_parent_review.task import SYSTEM_PROMPT as PARENT_PROMPT
from afk_parent_review.task import build_task as build_parent_review_task
from afk_plan.task import SYSTEM_PROMPT as PLAN_PROMPT
from afk_plan.task import build_task as build_plan_task
from afk_respond.contract import actionable_findings
from afk_respond.task import REPAIR_INSTRUCTIONS, RESPONSE_INSTRUCTIONS
from afk_respond.task import build_task as build_response_task
from afk_review.task import REVIEW_INSTRUCTIONS
from afk_review.task import build_task as build_review_task
from tests.test_plan_contract import planner_input


class RoleLocalInferenceTaskContractTest(unittest.TestCase):
    def test_conflict_requires_evidence_and_cannot_waive_selected_findings(self):
        selected = [{"finding_index": 0}]
        task = build_response_task({}, selected, "Consume the fixed producer contract.")
        value = {
            "summary": "Clarification required.",
            "finding_responses": [{"finding_index": 0, "response": "Not repaired."}],
            "contract_conflict": {
                "requirement": "No new calculator.",
                "evidence": "The required raw input is unavailable.",
            },
        }
        self.assertEqual(task.validator(json.dumps(value)), value)
        for conflict in (
            None,
            {},
            {"requirement": "x", "evidence": ""},
            {"requirement": "x", "evidence": "x" * 4001},
        ):
            with (
                self.subTest(conflict=str(conflict)[:50]),
                self.assertRaises(ResponseRejected),
            ):
                task.validator(json.dumps({**value, "contract_conflict": conflict}))
        with self.assertRaises(ResponseRejected):
            task.validator(json.dumps({**value, "finding_responses": []}))
        with self.assertRaises(ResponseRejected):
            build_response_task({}, [], "Validation repair").validator(
                json.dumps({**value, "finding_responses": []})
            )

    def test_trusted_task_renderers_have_explicit_snapshots(self):
        prompts = (
            ATTEMPT_INSTRUCTIONS,
            PLAN_PROMPT,
            PARENT_PROMPT,
            REVIEW_INSTRUCTIONS,
            ASSESSMENT_INSTRUCTIONS,
            RESPONSE_INSTRUCTIONS,
            REPAIR_INSTRUCTIONS,
        )
        self.assertEqual(
            [hashlib.sha256(prompt.encode()).hexdigest() for prompt in prompts],
            [
                "6231accc3f36a73e9d9dd99cfb1d6fed69eec5cfcce7d88230c1abe3e12984fb",
                "4a0366addfb3771fa4017b285de4ee0375248fa0dc789d9d9aa88cb6f85ed5a9",
                "e159e8dd84cab2bc4c45d208927d5e708f926e8dca4f76fbc18f525365614dd2",
                "aa9e281b25ed9da597a9f750b9f83a2db4aa8101cf08a38f14c88ca4d8912a77",
                "a9f4a3e0e3cbee2920101e5e783e4f448babb488f5eb9638508775da30666893",
                "acd8bafd14ba52b7fc6a8dc9d46858dd50328fcadd669608a34aca0924b795c7",
                "83ab33bf80cf6a60c2e55b6ce6b2c560c46bc04289c293455a32b7e357e1ee6b",
            ],
        )

    def test_each_role_main_delegates_the_complete_domain_task(self):
        root = Path(__file__).parents[1]
        for role in ("plan", "review", "assess", "respond", "parent_review"):
            main_source = (root / f"afk_{role}/__main__.py").read_text()
            task_source = (root / f"afk_{role}/task.py").read_text()
            with self.subTest(role=role):
                self.assertIn("build_task(", main_source)
                self.assertNotIn("def validate_response", main_source)
                self.assertNotIn("def validate_terminal_response", main_source)
                self.assertIn("trusted_instructions=", task_source)
                self.assertIn("untrusted_data=", task_source)
                self.assertIn("contract_version=", task_source)
                self.assertIn("validator=", task_source)

    def test_routing_roles_bind_current_prompt_data_capability_and_validator(self):
        request = planner_input()
        planner = build_plan_task(request)
        fan_in = {"schema_version": 2}
        parent = build_parent_review_task(fan_in)
        self.assertEqual((planner.contract_version, parent.contract_version), (4, 2))
        self.assertEqual(
            planner.untrusted_data,
            {
                **request,
                "source_project": "afk-pipeline",
                "source_criteria": [
                    {
                        "id": "criterion-1",
                        "source_text": request["parent"]["acceptance_criteria"],
                    }
                ],
            },
        )
        self.assertIs(parent.untrusted_data, fan_in)
        for task in (planner, parent):
            self.assertEqual(task.capability, Capability.NO_TOOLS)
            with self.assertRaises(ResponseRejected):
                task.validator({})

    def test_attempt_task_preserves_scope_reference_and_accepts_prose(self):
        assignment = {
            "objective": "Implement the owned change.",
            "work_base": "a" * 40,
            "related_work": {"path": "/frozen/context.jsonl", "sha256": "b" * 64},
            "worker": "inference",
            "workspace": "/workspace",
        }
        task = build_attempt_task(assignment)
        self.assertEqual(task.purpose, "attempt")
        self.assertEqual(task.contract_version, 2)
        self.assertEqual(task.capability, Capability.WRITE)
        self.assertEqual(
            task.untrusted_data,
            {
                key: assignment[key]
                for key in ("objective", "work_base", "related_work")
            },
        )
        self.assertEqual(
            task.validator("Implemented; checks passed."), "Implemented; checks passed."
        )
        for value in ("", "   ", {}, None):
            with self.subTest(value=value), self.assertRaises(ResponseRejected):
                task.validator(value)
        self.assertIn("Only included record IDs", task.trusted_instructions)
        self.assertIn("not additional work", task.trusted_instructions)

    def test_schema_repair_keeps_shared_evidence_per_selected_finding(self):
        review = {
            "findings": [
                {"title": "Pi accepts minimal unavailable cost"},
                {"title": "Add an unrelated adapter variant"},
                {"title": "Totals accept invocation-only provenance"},
                {"title": "Rejected speculative variant"},
            ]
        }
        assessment = {
            "summary": "Only existing owned variants.",
            "decisions": [
                {
                    "finding_index": index,
                    "defect_decision": decision,
                    "rationale": "Cost shape ownership.",
                    "scope": {
                        "kind": scope,
                        "rationale": "Only existing cost variants.",
                    },
                }
                for index, decision, scope in (
                    (0, "confirmed", "current"),
                    (1, "confirmed", "unknown"),
                    (2, "confirmed", "current"),
                    (3, "rejected", "current"),
                )
            ],
        }
        selected = actionable_findings(review, validate_assessment(review, assessment))
        task = build_response_task({}, selected, "Repair existing metrics cost intake.")
        self.assertEqual(task.contract_version, 5)
        self.assertEqual(
            [
                item["finding_index"]
                for item in task.untrusted_data["actionable_findings"]
            ],
            [0, 2],
        )
        self.assertEqual(set(task.untrusted_data), {"objective", "actionable_findings"})
        for clause in (
            "authoritative discriminator",
            "permitted and rejected variants",
            "public intake/parser seam",
            "Do not invent unsupported variants",
            "same explanation",
            "checks actually run",
        ):
            self.assertIn(clause, task.trusted_instructions)
        explanation = (
            "Cause: cost shape was selected by field presence instead of owner and "
            "adapter family. Change: separate Pi invocation, unsealed invocation, "
            "and totals cost. Checked: public intake accepts full Pi cost and "
            "minimal unsealed cost; rejects minimal Pi cost and provenance on "
            "totals. Unrelated adapter support is outside the assessed repair."
        )
        shared = {
            "summary": "One cost-shape repair addresses both findings.",
            "finding_responses": [
                {"finding_index": index, "response": explanation} for index in (0, 2)
            ],
        }
        self.assertEqual(task.validator(json.dumps(shared)), shared)
        for indices in ((0,), (0, 0, 2), (0, 1, 2), (0, True, 2)):
            with self.subTest(indices=indices), self.assertRaises(ResponseRejected):
                task.validator(
                    json.dumps(
                        {
                            **shared,
                            "finding_responses": [
                                {"finding_index": index, "response": explanation}
                                for index in indices
                            ],
                        }
                    )
                )
        # Retained free-form Response text remains valid. Prose quality is an
        # instruction/review concern, not a new keyword-based acceptance gate.
        retained = {
            "summary": "Fixed it.",
            "finding_responses": [
                {"finding_index": index, "response": "Updated the implementation."}
                for index in (0, 2)
            ],
        }
        self.assertEqual(task.validator(json.dumps(retained)), retained)

    def test_fixed_version_roles_bind_rendered_data_and_deterministic_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diff = root / "diff.patch"
            diff.write_text("diff content\n")
            review = {"findings": []}
            (root / "stdout.log").write_text("validation out")
            (root / "stderr.log").write_text("validation err")
            review_task = build_review_task(
                {"validation_directory": str(root)},
                {
                    "change": {
                        "objective": "objective",
                        "repository": {
                            "before": {"head": "a"},
                            "after": {"head": "b"},
                        },
                    },
                    "change_output": {},
                    "validation_input": {},
                    "validation": {},
                    "validation_stdout": "out",
                    "validation_stderr": "err",
                },
                diff,
                root,
                "b",
            )
            assessment_task = build_assessment_task(
                {"review_directory": str(root)},
                review,
                "objective",
                root,
                {
                    "input": {
                        "validation_directory": str(root),
                        "change_directory": str(root),
                    },
                    "change_output": {"change": "output"},
                    "validation_input": {"validation": "input"},
                    "validation": {"validation": "output"},
                    "validation_stdout": "validation out",
                    "validation_stderr": "validation err",
                },
            )
            self.assertEqual(
                assessment_task.untrusted_data["committed_change"],
                {"change": "output"},
            )
            self.assertEqual(
                assessment_task.untrusted_data["validation"],
                {
                    "input": {"validation": "input"},
                    "output": {"validation": "output"},
                    "stdout": "validation out",
                    "stderr": "validation err",
                },
            )
            response_task = build_response_task({}, [], "objective")

            for task in (review_task, assessment_task):
                with self.subTest(purpose=task.purpose):
                    for clause in (
                        "an explicitly required test or documentation deliverable",
                        "demonstrated maintenance or change cost",
                        "an applicable adopted standard",
                        "A runtime failure is not required",
                        "unsupported operating assumptions",
                        "Do not use a rejection quota",
                    ):
                        self.assertIn(clause, task.trusted_instructions)
                    self.assertNotIn(
                        'confirmed" only for a concrete, reachable defect',
                        task.trusted_instructions,
                    )

        expected = (
            (review_task, Capability.READ_ONLY),
            (assessment_task, Capability.READ_ONLY),
            (response_task, Capability.WRITE),
        )
        for task, capability in expected:
            with self.subTest(purpose=task.purpose):
                expected_version = {
                    "review": 10,
                    "finding_assessment": 6,
                    "feedback_response": 5,
                }[task.purpose]
                self.assertEqual(task.contract_version, expected_version)
                self.assertEqual(task.capability, capability)
                self.assertEqual(task.untrusted_data["objective"], "objective")
                with self.assertRaises(ResponseRejected):
                    task.validator(None)


if __name__ == "__main__":
    unittest.main()
