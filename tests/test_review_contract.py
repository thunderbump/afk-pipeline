import unittest
from pathlib import Path

from afk_review.contract import REVIEW_AUDIT, validate_review


class ReviewContractTest(unittest.TestCase):
    def review(self, audit=None):
        return {
            "summary": "Complete audit found no actionable defects.",
            "findings": [],
            **({"audit": REVIEW_AUDIT} if audit is None else {"audit": audit}),
        }

    def test_accepts_the_exact_declared_audit(self):
        value = self.review()
        self.assertIs(validate_review(value, Path("."), "unused"), value)

    def test_accepts_reordered_response_and_nested_object_fields(self):
        value = {
            "audit": {"scopes": list(REVIEW_AUDIT["scopes"]), "completed": True},
            "findings": [
                {
                    "scope_claim": {
                        "rationale": "The current objective owns this behavior.",
                        "kind": "current",
                    },
                    "locations": [{"line": 1, "path": "README.md"}],
                    "details": "A concrete problem occurs.",
                    "title": "Problem",
                    "lens": "behavior",
                }
            ],
            "summary": "Complete audit found one actionable defect.",
        }
        self.assertIs(validate_review(value, Path("."), "HEAD"), value)

    def test_rejects_missing_extra_or_malformed_audit(self):
        cases = {
            "missing response field": {"summary": "Clean.", "findings": []},
            "extra response field": {**self.review(), "proof": True},
            "missing audit field": self.review(
                {"scopes": list(REVIEW_AUDIT["scopes"])}
            ),
            "extra audit field": self.review(
                {
                    "completed": True,
                    "scopes": list(REVIEW_AUDIT["scopes"]),
                    "proof": True,
                }
            ),
            "reordered scopes": self.review(
                {
                    "completed": True,
                    "scopes": [
                        "acceptance_criteria",
                        "objective",
                        "reviewed_diff",
                        "supplied_evidence",
                    ],
                }
            ),
            "malformed completed": self.review(
                {"completed": 1, "scopes": list(REVIEW_AUDIT["scopes"])}
            ),
        }
        for name, value in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex((TypeError, ValueError), "malformed"),
            ):
                validate_review(value, Path("."), "unused")


if __name__ == "__main__":
    unittest.main()
