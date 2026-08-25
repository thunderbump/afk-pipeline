import os
import unittest
from pathlib import Path
from unittest import mock

from afk_agent import read_only_pi_command
from afk_config import INFERENCE_ROLE_DEFAULTS, effective_inference_roles
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
        self.assertEqual(
            selected["review"], {"model": "gpt-5.6-terra", "thinking": "medium"}
        )
        self.assertEqual(selected["feedback_response"]["thinking"], "high")
        self.assertEqual(
            selected["acceptance_planner"],
            INFERENCE_ROLE_DEFAULTS["acceptance_planner"],
        )

    def test_invalid_roles_models_and_thinking_are_rejected(self):
        invalid = (
            {"planner": {"model": "model"}},
            {"review": {"model": ""}},
            {"review": {"thinking": "extreme"}},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_inference_roles(value)

    def test_exact_argv_environment_override_precedes_selected_values(self):
        with mock.patch.dict(
            os.environ, {"AFK_REVIEW_AGENT_COMMAND": '["fixture", "--exact"]'}
        ):
            command = read_only_pi_command(
                "AFK_REVIEW_AGENT_COMMAND", "prompt", "terra", "high"
            )
        self.assertEqual(command, ["fixture", "--exact"])

    def test_selected_values_are_in_default_pi_argv(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            command = read_only_pi_command(
                "AFK_REVIEW_AGENT_COMMAND", "prompt", "terra", "high"
            )
        self.assertEqual(command[command.index("--model") + 1], "terra")
        self.assertEqual(command[command.index("--thinking") + 1], "high")


class FrozenCoordinatorRoleTest(unittest.TestCase):
    def test_role_settings_flow_from_frozen_request_to_each_stage(self):
        roles = {
            "review": {"model": "review-model", "thinking": "low"},
            "finding_assessment": {"model": "assess-model", "thinking": "high"},
            "feedback_response": {"model": "respond-model", "thinking": "minimal"},
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
