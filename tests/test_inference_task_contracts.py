import hashlib
import tempfile
import unittest
from pathlib import Path

from afk_assess.task import ASSESSMENT_INSTRUCTIONS
from afk_assess.task import build_task as build_assessment_task
from afk_inference import Capability, ResponseRejected
from afk_parent_review.task import SYSTEM_PROMPT as PARENT_PROMPT
from afk_parent_review.task import build_task as build_parent_review_task
from afk_plan.task import SYSTEM_PROMPT as PLAN_PROMPT
from afk_plan.task import build_task as build_plan_task
from afk_respond.task import REPAIR_INSTRUCTIONS, RESPONSE_INSTRUCTIONS
from afk_respond.task import build_task as build_response_task
from afk_review.task import REVIEW_INSTRUCTIONS
from afk_review.task import build_task as build_review_task
from tests.test_plan_contract import planner_input


class RoleLocalInferenceTaskContractTest(unittest.TestCase):
    def test_trusted_task_renderers_have_explicit_snapshots(self):
        prompts = (
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
                "bf02719b2b2fedb0d14c1cd5f611ef712a0c06dec82b11fe1339c8b8855b84b3",
                "e159e8dd84cab2bc4c45d208927d5e708f926e8dca4f76fbc18f525365614dd2",
                "bc6408b0456e90edf9bb0d8bef6e27f2cd909149a348493b37c6067b4c2aab79",
                "5eee337b538a643f6361c8a8927c5729f4c9f8e213606bdeee99a66beb9ee75e",
                "1bb5670cf37f6bf319e199db9a63e549efc8a566e16d9146bf386a8cc8c18c94",
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
        self.assertEqual((planner.contract_version, parent.contract_version), (2, 2))
        self.assertIs(planner.untrusted_data, request)
        self.assertIs(parent.untrusted_data, fan_in)
        for task in (planner, parent):
            self.assertEqual(task.capability, Capability.NO_TOOLS)
            with self.assertRaises(ResponseRejected):
                task.validator({})

    def test_fixed_version_roles_bind_rendered_data_and_deterministic_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diff = root / "diff.patch"
            diff.write_text("diff content\n")
            review = {"findings": []}
            review_task = build_review_task(
                {},
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
                {"review_directory": str(root)}, review, "objective", root
            )
            response_task = build_response_task({}, [], "objective")

        expected = (
            (review_task, Capability.READ_ONLY),
            (assessment_task, Capability.READ_ONLY),
            (response_task, Capability.WRITE),
        )
        for task, capability in expected:
            with self.subTest(purpose=task.purpose):
                expected_version = (
                    2 if task.purpose in {"review", "finding_assessment"} else 1
                )
                self.assertEqual(task.contract_version, expected_version)
                self.assertEqual(task.capability, capability)
                self.assertEqual(task.untrusted_data["objective"], "objective")
                with self.assertRaises(ResponseRejected):
                    task.validator(None)


if __name__ == "__main__":
    unittest.main()
