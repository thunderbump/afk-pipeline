import tempfile
import unittest
from pathlib import Path

from afk_assess.contract import validate_assessment
from afk_assess.task import build_task as build_assessment_task
from afk_related_work import (
    RelatedWorkError,
    build_snapshot,
    reference,
    validate_snapshot,
)
from afk_respond.contract import actionable_findings
from afk_review.contract import validate_finding, validate_scope_claim
from afk_review.task import (
    BEHAVIOR_INSTRUCTIONS,
    COMMON_INSTRUCTIONS,
    DESIGN_INSTRUCTIONS,
    OUTPUT_CONTRACT_INSTRUCTIONS,
    REVIEW_INSTRUCTIONS,
    STANDARDS_INSTRUCTIONS,
    compose_review_instructions,
)
from afk_review.task import build_task as build_review_task


class ScopeAwareReviewContractTest(unittest.TestCase):
    def test_review_packets_compose_in_declared_order(self):
        packets = (
            COMMON_INSTRUCTIONS,
            BEHAVIOR_INSTRUCTIONS,
            DESIGN_INSTRUCTIONS,
            STANDARDS_INSTRUCTIONS,
            OUTPUT_CONTRACT_INSTRUCTIONS,
        )
        self.assertEqual(compose_review_instructions(packets), "\n\n".join(packets))
        self.assertEqual(REVIEW_INSTRUCTIONS, compose_review_instructions())
        offsets = [REVIEW_INSTRUCTIONS.index(packet) for packet in packets]
        self.assertEqual(offsets, sorted(offsets))

    def test_review_scope_contract_covers_current_related_unknown_and_malformed(self):
        ids = {"sibling"}
        current = {"kind": "current", "rationale": "Owned by this objective."}
        related = {
            "kind": "related",
            "rationale": "The sibling record owns it.",
            "related_work_id": "sibling",
        }
        unknown = {"kind": "unknown", "rationale": "No owner is in evidence."}
        for claim in (current, related, unknown):
            with self.subTest(kind=claim["kind"]):
                self.assertIs(validate_scope_claim(claim, ids), claim)
        malformed = (
            {"kind": "related", "rationale": "Missing id."},
            {
                "kind": "related",
                "rationale": "Wrong id.",
                "related_work_id": "absent",
            },
            {
                "kind": "current",
                "rationale": "Extra id.",
                "related_work_id": "sibling",
            },
            {"kind": "unknown", "rationale": ""},
        )
        for claim in malformed:
            with self.subTest(claim=claim), self.assertRaises((TypeError, ValueError)):
                validate_scope_claim(claim, ids)

    def test_assessment_may_disagree_and_only_confirmed_current_is_actionable(self):
        findings = [
            {"title": "current"},
            {"title": "related"},
            {"title": "unknown"},
            {"title": "rejected"},
        ]
        review = {"findings": findings}
        decisions = [
            self.decision(0, "confirmed", "current"),
            self.decision(1, "confirmed", "related", "sibling"),
            self.decision(2, "confirmed", "unknown"),
            self.decision(3, "rejected", "current"),
        ]
        assessment = {"summary": "Independent result.", "decisions": decisions}
        self.assertIs(validate_assessment(review, assessment, {"sibling"}), assessment)
        self.assertEqual(
            [item["finding_index"] for item in actionable_findings(review, assessment)],
            [0],
        )
        self.assertEqual(decisions[1]["rationale"], "Independent defect rationale.")

    def test_no_findings_requires_no_decisions(self):
        value = {"summary": "Nothing to assess.", "decisions": []}
        self.assertIs(validate_assessment({"findings": []}, value), value)

    def test_task_builders_revalidate_frozen_related_work_before_using_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = {"task": {"id": "task", "title": "Current task"}}
            raw, facts = build_snapshot(records["task"], records.__getitem__)
            snapshot = root / "related-work.jsonl"
            snapshot.write_bytes(raw)
            related = reference(snapshot, facts)
            validate_snapshot(snapshot, related)
            snapshot.write_bytes(raw.replace(b"Current task", b"Changed task"))

            builders = (
                lambda: build_review_task(
                    {"related_work": related}, {}, root / "diff.patch", root, "HEAD"
                ),
                lambda: build_assessment_task(
                    {"related_work": related, "review_directory": str(root)},
                    {"findings": []},
                    "objective",
                    root,
                    {},
                ),
            )
            for builder in builders:
                with self.subTest(builder=builder), self.assertRaises(RelatedWorkError):
                    builder()

    def test_location_contract_rejects_extra_and_reordered_fields(self):
        base = {
            "lens": "behavior",
            "title": "Problem",
            "details": "Concrete defect.",
            "locations": [],
            "scope_claim": {
                "kind": "current",
                "rationale": "Owned by the objective.",
            },
        }
        malformed = (
            {"path": "README.md", "line": 1, "column": 2},
            {"line": 1, "path": "README.md"},
        )
        for location in malformed:
            finding = {**base, "locations": [location]}
            with (
                self.subTest(location=location),
                self.assertRaisesRegex(
                    ValueError, "location fields are malformed or out of order"
                ),
            ):
                validate_finding(finding, Path.cwd(), "HEAD")

    @staticmethod
    def decision(index, defect, scope, related_id=None):
        scope_value = {
            "kind": scope,
            "rationale": "Independent ownership rationale.",
        }
        if related_id is not None:
            scope_value["related_work_id"] = related_id
        return {
            "finding_index": index,
            "defect_decision": defect,
            "rationale": "Independent defect rationale.",
            "scope": scope_value,
        }


if __name__ == "__main__":
    unittest.main()
