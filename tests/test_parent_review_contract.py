import unittest

from afk_parent_review.contract import validate_follow_up
from afk_parent_review.task import CAPABILITY_SYSTEM_PROMPT


class ParentReviewCapabilityContractTest(unittest.TestCase):
    def fan_in(self):
        return {
            "schema_version": 2,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": "The unavailable operation is performed.",
                    "statement": "Perform the unavailable operation.",
                }
            ],
            "catalog": {
                "schema_version": 2,
                "projects": [
                    {
                        "slug": "example",
                        "routes": [
                            {
                                "owner": "Outside operator",
                                "executor": "outside_help",
                                "outside_help_reason": "physical_action",
                                "evidence_route": "external_check",
                                "phases": ["closure"],
                            }
                        ],
                    }
                ],
            },
            "children": [],
        }

    def follow_up(self, evidence_route="external_check"):
        return {
            "local_id": "outside-follow-up",
            "title": "Perform the unavailable operation",
            "objective": "Use the unavailable physical capability and record the work.",
            "criteria": ["criterion-1"],
            "project": "example",
            "owner": "Outside operator",
            "phase": "closure",
            "executor": "outside_help",
            "outside_help_reason": "physical_action",
            "evidence_route": evidence_route,
            "depends_on": [],
        }

    def test_v2_outside_help_follow_up_requires_external_check(self):
        accepted = validate_follow_up(self.follow_up(), ["criterion-1"], self.fan_in())
        self.assertEqual(accepted["evidence_route"], "external_check")

        with self.assertRaisesRegex(
            ValueError, "outside_help evidence_route must be external_check"
        ):
            validate_follow_up(
                self.follow_up("human_attestation"),
                ["criterion-1"],
                self.fan_in(),
            )

    def test_capability_prompt_has_only_capability_and_performed_work_semantics(self):
        prompt = CAPABILITY_SYSTEM_PROMPT.lower()
        self.assertIn("unavailable", prompt)
        self.assertIn("work performed", prompt)
        self.assertIn("external_check", prompt)
        self.assertNotIn("approval", prompt)
        self.assertNotIn("waiver", prompt)


if __name__ == "__main__":
    unittest.main()
