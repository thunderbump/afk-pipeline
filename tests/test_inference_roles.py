import unittest
from pathlib import Path

from afk_assess.__main__ import validate_input as validate_assessment_input
from afk_coordinate.contract import validate_request
from afk_respond.contract import validate_input as validate_response_input
from afk_review.__main__ import validate_input as validate_review_input


class RuntimeOwnedInferenceRoleTest(unittest.TestCase):
    def test_role_modules_have_no_legacy_inference_execution(self):
        root = Path(__file__).parents[1]
        for role in (
            "afk_plan",
            "afk_review",
            "afk_assess",
            "afk_respond",
            "afk_parent_review",
        ):
            source = (root / role / "__main__.py").read_text()
            with self.subTest(role=role):
                self.assertIn("invoke(", source)
                self.assertNotIn("run_command", source)
                self.assertNotIn("afk_agent", source)
                self.assertNotIn("AGENT_COMMAND", source)
                self.assertNotIn("inference=", source)

    def test_role_inputs_reject_obsolete_policy_overrides(self):
        override = {"model": "other", "thinking": "high"}
        cases = (
            (
                validate_review_input,
                {
                    "schema_version": 1,
                    "workspace": "/tmp/workspace",
                    "change_directory": "/tmp/change",
                    "validation_directory": "/tmp/validation",
                    "timeout_seconds": 1,
                    "inference": override,
                },
            ),
            (
                validate_assessment_input,
                {
                    "schema_version": 1,
                    "workspace": "/tmp/workspace",
                    "review_directory": "/tmp/review",
                    "timeout_seconds": 1,
                    "inference": override,
                },
            ),
            (
                validate_response_input,
                {
                    "schema_version": 1,
                    "workspace": "/tmp/workspace",
                    "assessment_directory": "/tmp/assessment",
                    "timeout_seconds": 1,
                    "inference": override,
                },
            ),
        )
        for validator, value in cases:
            with (
                self.subTest(validator=validator.__module__),
                self.assertRaisesRegex(ValueError, "cannot override inference policy"),
            ):
                validator(value)

    def test_coordinator_rejects_obsolete_role_configuration(self):
        request = {
            "schema_version": 1,
            "assignment_path": "/tmp/assignment.json",
            "validation": {"command": ["validate"], "timeout_seconds": 1},
            "agent_timeout_seconds": 1,
            "max_responses": 0,
            "inference_roles": {},
        }
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_request(request)


if __name__ == "__main__":
    unittest.main()
