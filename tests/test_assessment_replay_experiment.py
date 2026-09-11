"""Check replay provenance and immutable observation handling without inference."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from afk_assess.task import ASSESSMENT_INSTRUCTIONS
from afk_inference import FixtureAdapter, ScriptedResult
from afk_review.contract import REVIEW_AUDIT
from experiments.assessment_replay import materialize, prepare, run_call
from experiments.split_review import diff, git


class AssessmentReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "sample.txt").write_text("before\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "sample.txt").write_text("after\n")
        git(self.repo, "commit", "-qam", "candidate")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.finding = {
            "lens": "behavior",
            "title": "Required behavior is missing",
            "details": "The required positive case is absent.",
            "locations": [{"path": "sample.txt", "line": 1}],
            "scope_claim": {"kind": "current", "rationale": "The objective owns it."},
        }
        invocation = self.root / "invocation.json"
        invocation.write_text(
            json.dumps(
                {
                    "purpose": "finding_assessment",
                    "task_contract_version": 5,
                    "execution_root": str(self.repo),
                    "prompt": {
                        "trusted_task_instructions": ASSESSMENT_INSTRUCTIONS,
                        "untrusted_task_data": {
                            "objective": "Required behavior",
                            "related_work": [],
                            "reviewed_diff": diff(self.repo, base, head),
                            "committed_change": {
                                "change": {
                                    "repository": {
                                        "before": {"head": base},
                                        "after": {"head": head},
                                    }
                                }
                            },
                        },
                    },
                }
            )
        )
        sources = []
        for arm in ("behavior", "design"):
            path = self.root / f"{arm}.json"
            finding = {**self.finding, "lens": arm}
            path.write_text(
                json.dumps(
                    {
                        "outcome": "succeeded",
                        "arm": arm,
                        "before": {"head": head},
                        "after": {"head": head},
                        "workspace_unchanged": True,
                        "packet_unchanged": True,
                        "review": {
                            "summary": "Review complete",
                            "findings": [finding],
                            "audit": REVIEW_AUDIT,
                        },
                    }
                )
            )
            sources.append(str(path))
        self.item = {
            "id": "case",
            "repo": str(self.repo),
            "head": head,
            "invocation": str(invocation),
            "reviews": sources,
            "hashes": {
                p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                for p in [str(invocation), *sources]
            },
        }

    def test_aggregation_preserves_duplicates_and_original_indices(self):
        case = prepare(self.item)
        self.assertEqual([x["finding_index"] for x in case["observations"]], [0, 1])
        self.assertEqual(
            [x["source_finding_index"] for x in case["observations"]], [0, 0]
        )
        self.assertEqual(case["data"]["findings"][0], self.finding)
        self.assertEqual(
            case["data"]["findings"][1], {**self.finding, "lens": "design"}
        )
        before = copy.deepcopy(case["data"])
        target = self.root / "packet-copy"
        target.mkdir()
        data, _ = materialize(case, target, Path("/candidate"))
        self.assertEqual(case["data"], before)
        self.assertNotIn("observations", data)
        self.assertNotIn("judgment", json.dumps(data))

    def test_mutated_review_rejected(self):
        source = Path(self.item["reviews"][0])
        source.write_text(source.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "frozen input changed"):
            prepare(self.item)

    def test_runtime_returns_one_decision_for_each_duplicate_observation(self):
        output = {
            "summary": "One shared defect, two observations.",
            "decisions": [
                {
                    "finding_index": i,
                    "defect_decision": "confirmed",
                    "rationale": "Required behavior is absent.",
                    "scope": {
                        "kind": "current",
                        "rationale": "Current objective owns it.",
                    },
                }
                for i in range(2)
            ],
        }
        adapter = FixtureAdapter((ScriptedResult(response=json.dumps(output)),))
        result = run_call(
            prepare(self.item), self.root / "call", self.repo, adapter, 30
        )
        self.assertEqual(result["outcome"], "succeeded", result)
        self.assertEqual(result["assessment"], output)
        self.assertTrue(result["workspace_unchanged"])
        self.assertTrue(result["packet_unchanged"])

    def test_comparison_changes_only_instructions(self):
        output = {
            "summary": "Reviewed",
            "decisions": [
                {
                    "finding_index": i,
                    "defect_decision": "confirmed",
                    "rationale": "Required behavior is absent.",
                    "scope": {
                        "kind": "current",
                        "rationale": "Current objective owns it.",
                    },
                }
                for i in range(2)
            ],
        }
        packets = []
        for variant in ("baseline", "evidence-first"):
            directory = self.root / variant
            result = run_call(
                prepare(self.item),
                directory,
                self.repo,
                FixtureAdapter((ScriptedResult(response=json.dumps(output)),)),
                30,
                variant,
                2,
            )
            self.assertEqual(result["outcome"], "succeeded")
            self.assertEqual(result["variant"], variant)
            packets.append(
                json.loads((directory / "inference/invocation.json").read_text())
            )
        self.assertEqual(
            packets[0]["prompt"]["untrusted_task_data"],
            packets[1]["prompt"]["untrusted_task_data"],
        )
        self.assertTrue(
            packets[1]["prompt"]["trusted_task_instructions"].endswith(
                packets[0]["prompt"]["trusted_task_instructions"]
            )
        )

    def test_retained_assessment_uses_original_review(self):
        path = Path(self.item["invocation"])
        invocation = json.loads(path.read_text())
        data = invocation["prompt"]["untrusted_task_data"]
        data["review"] = {
            "summary": "Original review",
            "findings": [self.finding],
            "audit": REVIEW_AUDIT,
        }
        data["findings"] = [self.finding]
        path.write_text(json.dumps(invocation))
        self.item.update(
            source_kind="retained_assessment",
            reviews=[],
            hashes={str(path): hashlib.sha256(path.read_bytes()).hexdigest()},
        )
        case = prepare(self.item)
        self.assertEqual(case["data"]["review"], data["review"])
        self.assertEqual(case["observations"][0]["source"], str(path))
