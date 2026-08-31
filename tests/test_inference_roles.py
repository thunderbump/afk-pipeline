import unittest
from pathlib import Path

from afk_config import (
    INFERENCE_ROLE_DEFAULTS,
    effective_inference_roles,
    validate_inference_setting,
)
from afk_coordinate.__main__ import assessment_input, response_input, review_input
from afk_coordinate.contract import validate_request


class InferenceRoleConfigurationTest(unittest.TestCase):
    def test_defaults_and_partial_per_role_selection(self):
        self.assertEqual(effective_inference_roles(), INFERENCE_ROLE_DEFAULTS)
        selected = effective_inference_roles(
            {
                "review": {"model": "gpt-5.6-terra"},
                "feedback_response": {"thinking": "high"},
            }
        )
        self.assertEqual(selected["review"]["model"], "gpt-5.6-terra")
        self.assertEqual(selected["review"]["thinking"], "medium")
        self.assertEqual(selected["review"]["adapter_family"], "pi")
        self.assertEqual(selected["review"]["adapter_contract_version"], 1)
        self.assertEqual(selected["feedback_response"]["thinking"], "high")
        self.assertEqual(
            selected["acceptance_planner"],
            INFERENCE_ROLE_DEFAULTS["acceptance_planner"],
        )

    def test_invalid_roles_models_and_thinking_are_rejected(self):
        invalid = (
            None,
            {"planner": {"model": "model"}},
            {"review": {"model": ""}},
            {"review": {"thinking": "extreme"}},
            {"review": {"adapter_family": "fixture"}},
            {"review": {"adapter_contract_version": 2}},
            {"review": {"adapter_contract_version": True}},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_inference_roles(value)

    def test_boolean_contract_version_is_rejected_in_durable_settings(self):
        with self.assertRaises(ValueError):
            validate_inference_setting(
                {
                    "adapter_family": "pi",
                    "adapter_contract_version": True,
                    "model": "model",
                    "thinking": "low",
                }
            )

    def test_read_only_roles_do_not_construct_provider_processes(self):
        root = Path(__file__).parents[1]
        for role in ("afk_review", "afk_assess"):
            source = (root / role / "__main__.py").read_text()
            self.assertIn("requested_capability=Capability.READ_ONLY", source)
            self.assertIn("execution_root=workspace", source)
            self.assertNotIn("read_only_pi_command", source)
            self.assertNotIn("AFK_REVIEW_AGENT_COMMAND", source)
            self.assertNotIn("AFK_ASSESS_AGENT_COMMAND", source)


class FrozenCoordinatorRoleTest(unittest.TestCase):
    def test_role_settings_flow_from_frozen_request_to_each_stage(self):
        adapter = {"adapter_family": "pi", "adapter_contract_version": 1}
        roles = {
            "review": {**adapter, "model": "review-model", "thinking": "low"},
            "finding_assessment": {
                **adapter,
                "model": "assess-model",
                "thinking": "high",
            },
            "feedback_response": {
                **adapter,
                "model": "respond-model",
                "thinking": "minimal",
            },
        }
        request = {
            "schema_version": 1,
            "assignment_path": "/tmp/assignment.json",
            "validation": {"command": ["validate"], "timeout_seconds": 1},
            "agent_timeout_seconds": 2,
            "max_responses": 1,
            "inference_roles": roles,
        }
        validate_request(request)
        assignment = {"workspace": "/tmp/workspace"}
        state = {
            "history": [
                {"component": "change", "directory": "change", "outcome": "completed"},
                {
                    "component": "validation",
                    "directory": "validation",
                    "outcome": "passed",
                },
                {"component": "review", "directory": "review", "outcome": "completed"},
                {
                    "component": "assessment",
                    "directory": "assessment",
                    "outcome": "completed",
                },
            ]
        }
        root = Path("/tmp/coordinator")
        self.assertEqual(
            review_input(request, assignment, state, root)["inference"], roles["review"]
        )
        self.assertEqual(
            assessment_input(request, assignment, state, root)["inference"],
            roles["finding_assessment"],
        )
        self.assertEqual(
            response_input(request, assignment, state, root)["inference"],
            roles["feedback_response"],
        )


if __name__ == "__main__":
    unittest.main()
