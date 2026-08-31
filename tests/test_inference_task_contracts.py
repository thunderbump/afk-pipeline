import hashlib
import tempfile
import unittest
from pathlib import Path

from afk_assess.task import ASSESSMENT_INSTRUCTIONS
from afk_assess.task import build_task as build_assessment_task
from afk_inference import Capability, ResponseRejected
from afk_parent_review.task import CAPABILITY_SYSTEM_PROMPT as PARENT_V2_PROMPT
from afk_parent_review.task import SYSTEM_PROMPT as PARENT_V1_PROMPT
from afk_parent_review.task import build_task as build_parent_review_task
from afk_plan.task import CAPABILITY_SYSTEM_PROMPT as PLAN_V2_PROMPT
from afk_plan.task import SYSTEM_PROMPT as PLAN_V1_PROMPT
from afk_plan.task import build_task as build_plan_task
from afk_respond.task import REPAIR_INSTRUCTIONS, RESPONSE_INSTRUCTIONS
from afk_respond.task import build_task as build_response_task
from afk_review.task import REVIEW_INSTRUCTIONS
from afk_review.task import build_task as build_review_task
from tests.test_plan_contract import planner_input


class RoleLocalInferenceTaskContractTest(unittest.TestCase):
    def test_trusted_task_renderers_have_explicit_snapshots(self):
        prompts = (
            PLAN_V1_PROMPT,
            PLAN_V2_PROMPT,
            PARENT_V1_PROMPT,
            PARENT_V2_PROMPT,
            REVIEW_INSTRUCTIONS,
            ASSESSMENT_INSTRUCTIONS,
            RESPONSE_INSTRUCTIONS,
            REPAIR_INSTRUCTIONS,
        )
        self.assertEqual(
            [hashlib.sha256(prompt.encode()).hexdigest() for prompt in prompts],
            [
                "36e355ec9444e97ba47b2838f3926274b9c537bd482796c3687cdaa44980bc3c",
                "bf02719b2b2fedb0d14c1cd5f611ef712a0c06dec82b11fe1339c8b8855b84b3",
                "213f00250cd1ee54436689e0ecccecb17fb4914c1ba369f4f20e5b364f2ff837",
                "e159e8dd84cab2bc4c45d208927d5e708f926e8dca4f76fbc18f525365614dd2",
                "f56ac9a3e09acaa3f686df68b6fe08fd4f73a30a282b72f75b9b07a4651371a7",
                "768cf586297200e617961dbbb60f365b605753adf4a8bcc6639adabe93d41666",
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

    def test_versioned_roles_select_prompt_data_capability_and_validator_together(self):
        request = planner_input()
        v1 = build_plan_task(request)
        request_v2 = {**request, "schema_version": 2}
        v2 = build_plan_task(request_v2)
        self.assertEqual((v1.contract_version, v2.contract_version), (1, 2))
        self.assertIs(v1.untrusted_data, request)
        self.assertIs(v2.untrusted_data, request_v2)
        self.assertEqual(v1.capability, Capability.NO_TOOLS)
        self.assertNotEqual(v1.trusted_instructions, v2.trusted_instructions)

        fan_in_v1 = {"schema_version": 1}
        fan_in_v2 = {"schema_version": 2}
        parent_v1 = build_parent_review_task(fan_in_v1)
        parent_v2 = build_parent_review_task(fan_in_v2)
        self.assertEqual(
            (parent_v1.contract_version, parent_v2.contract_version), (1, 2)
        )
        self.assertNotEqual(
            parent_v1.trusted_instructions, parent_v2.trusted_instructions
        )
        for task in (v1, v2, parent_v1, parent_v2):
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
                self.assertEqual(task.contract_version, 1)
                self.assertEqual(task.capability, capability)
                self.assertEqual(task.untrusted_data["objective"], "objective")
                with self.assertRaises(ResponseRejected):
                    task.validator(None)


if __name__ == "__main__":
    unittest.main()
