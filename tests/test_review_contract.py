import json
import tempfile
import unittest
from pathlib import Path

from afk_review.contract import (
    REVIEW_AUDIT,
    validate_output_projection,
    validate_review,
)


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

    def test_split_projection_requires_all_ordered_authentic_invocations(self):
        aggregate = {
            "summary": (
                "Behavior: Complete audit found no actionable defects.\n"
                "Design: Complete audit found no actionable defects.\n"
                "Standards: Complete audit found no actionable defects."
            ),
            "findings": [],
            "audit": REVIEW_AUDIT,
        }
        invocation = lambda lens: {
            "lens": lens,
            "outcome": "succeeded",
            "process": {"exit_code": 0, "signal": None},
            "agent": {"status": "completed"},
            "review": self.review(),
            "artifacts": {
                "events": f"reviewers/{lens}/events.jsonl",
                "stderr": f"reviewers/{lens}/stderr.log",
                "inference": f"reviewers/{lens}/inference",
            },
        }
        output = {
            "review": aggregate,
            "review_mode": "split",
            "review_invocations": [
                invocation(lens) for lens in ("behavior", "design", "standards")
            ],
            "finding_provenance": [],
        }
        self.assertIs(
            validate_output_projection(output, Path("."), "unused"), aggregate
        )
        output["review_invocations"].reverse()
        with self.assertRaisesRegex(ValueError, "projection"):
            validate_output_projection(output, Path("."), "unused")

    def test_split_projection_is_bound_to_each_accepted_invocation_receipt(self):
        aggregate = {
            "summary": "\n".join(
                f"{lens.capitalize()}: Complete audit found no actionable defects."
                for lens in ("behavior", "design", "standards")
            ),
            "findings": [],
            "audit": REVIEW_AUDIT,
        }
        invocations = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for lens in ("behavior", "design", "standards"):
                review = self.review()
                inference = directory / "reviewers" / lens / "inference"
                inference.mkdir(parents=True)
                (inference / "receipt.json").write_text(
                    json.dumps(
                        {
                            "outcome": "succeeded",
                            "protocol": {"status": "accepted"},
                            "terminal_response": review,
                        }
                    )
                )
                invocations.append(
                    {
                        "lens": lens,
                        "outcome": "succeeded",
                        "process": {"exit_code": 0, "signal": None},
                        "agent": {"status": "completed"},
                        "review": review,
                        "artifacts": {
                            "events": f"reviewers/{lens}/events.jsonl",
                            "stderr": f"reviewers/{lens}/stderr.log",
                            "inference": f"reviewers/{lens}/inference",
                        },
                    }
                )
            output = {
                "review": aggregate,
                "review_mode": "split",
                "review_invocations": invocations,
                "finding_provenance": [],
            }
            self.assertIs(
                validate_output_projection(
                    output, Path("."), "unused", review_directory=directory
                ),
                aggregate,
            )
            output["review_invocations"][1]["review"] = {
                **self.review(),
                "summary": "Altered after the invocation.",
            }
            with self.assertRaisesRegex(ValueError, "receipt disagrees"):
                validate_output_projection(
                    output, Path("."), "unused", review_directory=directory
                )

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
