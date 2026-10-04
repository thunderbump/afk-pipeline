import unittest

from afk_coordinate.contract import validate_request
from afk_respond.contract import validate_input as validate_response_input


class RuntimeOwnedInferenceRoleTest(unittest.TestCase):
    def test_role_inputs_reject_obsolete_policy_overrides(self):
        override = {"model": "other", "thinking": "high"}
        cases = (
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
