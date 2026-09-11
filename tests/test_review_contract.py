import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_evidence.access import MAX_VALIDATION_LOG_BYTES
from afk_inference.runtime import PiAdapter
from afk_review.contract import (
    REVIEW_AUDIT,
    validate_invocation_receipts,
    validate_output_projection,
    validate_review,
)
from afk_review.task import build_task


class ReviewContractTest(unittest.TestCase):
    def review(self, audit=None):
        return {
            "summary": "Complete audit found no actionable defects.",
            "findings": [],
            **({"audit": REVIEW_AUDIT} if audit is None else {"audit": audit}),
        }

    def write_split_invocation(self, inference, lens):
        marker = (
            f"This is the isolated {lens} lens invocation. Report only findings "
            f'with lens "{lens}".'
        )
        invocation = {
            "schema_version": 1,
            "purpose": "review",
            "task_contract_version": 8,
            "prompt": {
                "purpose": "review",
                "task_contract_version": 8,
                "trusted_task_instructions": marker,
            },
            "requested_capability": "READ_ONLY",
        }
        raw = json.dumps(invocation).encode()
        (inference / "invocation.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

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
                invocation_hash = self.write_split_invocation(inference, lens)
                (inference / "receipt.json").write_text(
                    json.dumps(
                        {
                            "outcome": "succeeded",
                            "protocol": {"status": "accepted"},
                            "terminal_response": json.dumps(review),
                            "hashes": {"invocation_sha256": invocation_hash},
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

    def test_prepared_split_tasks_retain_receipt_lens_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            diff = directory / "review.diff"
            diff.write_text("diff --git a/a b/a\n")
            context_file = directory / "work.diff"
            context_file.write_text("complete work\n")
            evidence = {
                "change": {
                    "objective": "Review the complete prepared work.",
                    "repository": {
                        "before": {"head": "before"},
                        "after": {"head": "after"},
                    },
                },
                "change_output": {},
                "validation_input": {},
                "validation": {},
                "validation_stdout": "",
                "validation_stderr": "",
                "work_context": {
                    "work_base": "base",
                    "files": {
                        "work_diff": {"path": context_file.name},
                        "repair_diff": {"path": context_file.name},
                    },
                },
            }
            review = self.review()
            invocations = []
            for lens in ("behavior", "design", "standards"):
                with mock.patch("afk_review.task.git", return_value="1 file changed"):
                    task = build_task({}, evidence, diff, directory, "after", lens=lens)
                inference = directory / "reviewers" / lens / "inference"
                inference.mkdir(parents=True)
                invocation = {
                    "schema_version": 1,
                    "purpose": task.purpose,
                    "task_contract_version": task.contract_version,
                    "prompt": {
                        "purpose": task.purpose,
                        "task_contract_version": task.contract_version,
                        "trusted_task_instructions": task.trusted_instructions,
                    },
                    "requested_capability": task.capability.value,
                }
                raw = json.dumps(invocation).encode()
                (inference / "invocation.json").write_bytes(raw)
                (inference / "receipt.json").write_text(
                    json.dumps(
                        {
                            "outcome": "succeeded",
                            "protocol": {"status": "accepted"},
                            "terminal_response": json.dumps(review),
                            "hashes": {
                                "invocation_sha256": hashlib.sha256(raw).hexdigest()
                            },
                        }
                    )
                )
                invocations.append(
                    {"lens": lens, "outcome": "succeeded", "review": review}
                )

            validate_invocation_receipts(
                {
                    "outcome": "completed",
                    "review_mode": "split",
                    "review_invocations": invocations,
                },
                directory,
            )

    def test_receipt_binding_accepts_supported_boundary_pi_split_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inference = directory / "reviewers" / "behavior" / "inference"
            inference.mkdir(parents=True)
            marker = (
                "This is the isolated behavior lens invocation. Report only findings "
                'with lens "behavior".'
            )
            # Exercise Pi's real rendering: JSON escaping doubles each retained
            # newline-filled log, then the rendered prompt base64-encodes that
            # structured task data. Both logs are at the producer's supported
            # boundary rather than at a small proxy size.
            retained_log = "\n" * MAX_VALIDATION_LOG_BYTES
            prompt = PiAdapter(model="test-model", thinking="high").render(
                {
                    "system": "Review system instructions.",
                    "purpose": "review",
                    "task_contract_version": 8,
                    "trusted_task_instructions": marker,
                    "untrusted_task_data": {
                        "validation": {
                            "stdout": retained_log,
                            "stderr": retained_log,
                        }
                    },
                }
            )
            invocation = {
                "schema_version": 1,
                "purpose": "review",
                "task_contract_version": 8,
                "prompt": prompt,
                "requested_capability": "READ_ONLY",
            }
            raw = json.dumps(invocation).encode()
            self.assertGreater(len(raw), 128 * 1024 * 1024)
            (inference / "invocation.json").write_bytes(raw)
            review = self.review()
            (inference / "receipt.json").write_text(
                json.dumps(
                    {
                        "outcome": "succeeded",
                        "protocol": {"status": "accepted"},
                        "terminal_response": json.dumps(review),
                        "hashes": {
                            "invocation_sha256": hashlib.sha256(raw).hexdigest()
                        },
                    }
                )
            )

            invocations = [
                {"lens": "behavior", "outcome": "succeeded", "review": review}
            ]
            for lens in ("design", "standards"):
                lens_inference = directory / "reviewers" / lens / "inference"
                lens_inference.mkdir(parents=True)
                invocation_hash = self.write_split_invocation(lens_inference, lens)
                (lens_inference / "receipt.json").write_text(
                    json.dumps(
                        {
                            "outcome": "succeeded",
                            "protocol": {"status": "accepted"},
                            "terminal_response": json.dumps(review),
                            "hashes": {"invocation_sha256": invocation_hash},
                        }
                    )
                )
                invocations.append(
                    {"lens": lens, "outcome": "succeeded", "review": review}
                )

            validate_invocation_receipts(
                {
                    "outcome": "completed",
                    "review_mode": "split",
                    "review_invocations": invocations,
                },
                directory,
            )

    def test_receipt_binding_decodes_json_text_and_allows_failed_split_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            invocations = []
            cases = (
                ("behavior", "succeeded", self.review()),
                ("design", "timed_out", None),
            )
            for lens, outcome, review in cases:
                inference = directory / "reviewers" / lens / "inference"
                inference.mkdir(parents=True)
                invocation_hash = self.write_split_invocation(inference, lens)
                receipt = {
                    "outcome": outcome,
                    "protocol": {
                        "status": "accepted" if outcome == "succeeded" else "timed_out"
                    },
                    "terminal_response": (
                        json.dumps(review, separators=(",", ":"))
                        if review is not None
                        else None
                    ),
                    "hashes": {"invocation_sha256": invocation_hash},
                }
                (inference / "receipt.json").write_text(json.dumps(receipt))
                invocations.append({"lens": lens, "outcome": outcome, "review": review})
            output = {
                "outcome": "timed_out",
                "review_mode": "split",
                "review_invocations": invocations,
            }

            from afk_review.contract import validate_invocation_receipts

            validate_invocation_receipts(output, directory)

            # An authenticated receipt after the first unsuccessful lens is
            # still impossible for the sequential executor and must not make a
            # tampered failed projection exportable.
            inference = directory / "reviewers/standards/inference"
            inference.mkdir(parents=True)
            invocation_hash = self.write_split_invocation(inference, "standards")
            (inference / "receipt.json").write_text(
                json.dumps(
                    {
                        "outcome": "succeeded",
                        "protocol": {"status": "accepted"},
                        "terminal_response": json.dumps(self.review()),
                        "hashes": {"invocation_sha256": invocation_hash},
                    }
                )
            )
            invocations.append(
                {"lens": "standards", "outcome": "succeeded", "review": self.review()}
            )
            with self.assertRaisesRegex(ValueError, "projection is malformed"):
                validate_invocation_receipts(output, directory)
            invocations.pop()

            behavior_receipt = directory / "reviewers/behavior/inference/receipt.json"
            receipt = json.loads(behavior_receipt.read_text())
            receipt["terminal_response"] = self.review()
            behavior_receipt.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "receipt disagrees"):
                validate_invocation_receipts(output, directory)
            receipt["terminal_response"] = json.dumps(
                self.review(), separators=(",", ":")
            )
            behavior_receipt.write_text(json.dumps(receipt))
            invocations[0]["review"] = {
                **self.review(),
                "summary": "Not the accepted response.",
            }
            with self.assertRaisesRegex(ValueError, "receipt disagrees"):
                validate_invocation_receipts(output, directory)

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
