"""Regression checks for the caller-owned Review context experiment."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_inference import FixtureAdapter, ScriptedResult
from afk_review.contract import REVIEW_AUDIT
from experiments.review_context import (
    diff,
    git,
    prepare_case,
    run_call,
    task_data,
    workspace_state,
)


class ReviewContextExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "feature.py").write_text("value = 1\n")
        self.base = self.commit("base")
        (self.repo / "feature.py").write_text("value = 2\n")
        self.before = self.commit("feature")
        (self.repo / "repair.py").write_text("repaired = True\n")
        self.head = self.commit("repair")
        self.data = {
            "objective": "Deliver the feature",
            "reviewed_commits": {"before": self.before, "after": self.head},
            "reviewed_diff": diff(self.repo, self.before, self.head),
            "committed_change": {"workspace": str(self.repo)},
            "validation": {"outcome": "passed"},
            "related_work": [],
        }
        self.preparation = self.root / "preparation.json"
        self.preparation.write_text(
            json.dumps({"repository": {"base_commit": self.base}})
        )
        self.invocation = self.root / "invocation.json"
        self.invocation.write_text(
            json.dumps(
                {
                    "purpose": "review",
                    "execution_root": str(self.repo),
                    "prompt": {
                        "trusted_task_instructions": "Review read-only",
                        "untrusted_task_data": self.data,
                    },
                }
            )
        )
        self.item = {
            "id": "case",
            "preparation": str(self.preparation),
            "invocation": str(self.invocation),
        }
        self.case = prepare_case(self.repo, self.item)
        self.review = {"summary": "No findings", "findings": [], "audit": REVIEW_AUDIT}

    def commit(self, message):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def test_full_context_restores_feature_after_tiny_repair(self):
        latest = task_data(self.case, "latest", self.repo)
        full = task_data(self.case, "full", self.repo)
        self.assertNotIn("feature.py", latest["reviewed_diff"])
        self.assertIn("feature.py", full["reviewed_diff"])
        self.assertIn("repair.py", full["reviewed_diff"])
        self.assertEqual(full["reviewed_commits"]["before"], self.base)
        for key in self.data.keys() - {"reviewed_diff", "reviewed_commits"}:
            self.assertEqual(full[key], latest[key])
        self.assertEqual(
            self.case["invocation"]["prompt"]["untrusted_task_data"], self.data
        )

    def test_workspace_rebinding_does_not_change_original_evidence(self):
        original = copy.deepcopy(self.case)
        value = task_data(self.case, "latest", self.root / "other")
        self.assertEqual(
            value["committed_change"]["workspace"], str(self.root / "other")
        )
        self.assertEqual(self.case, original)

    def test_inconsistent_retained_diff_is_rejected(self):
        value = json.loads(self.invocation.read_text())
        value["prompt"]["untrusted_task_data"]["reviewed_diff"] = "wrong"
        self.invocation.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "disagrees"):
            prepare_case(self.repo, self.item)

    def test_fixture_uses_real_runtime_and_validator_without_source_changes(self):
        original = self.invocation.read_bytes()
        result = run_call(
            self.case,
            "full",
            1,
            self.repo,
            self.root / "call",
            FixtureAdapter((ScriptedResult(response=json.dumps(self.review)),)),
            5,
        )
        self.assertEqual(result["outcome"], "succeeded")
        self.assertTrue(result["workspace_unchanged"])
        self.assertEqual(result["review"], self.review)
        self.assertEqual(self.invocation.read_bytes(), original)
        self.assertTrue((self.root / "call/inference/receipt.json").exists())

    def test_invalid_review_is_retained_as_rejected(self):
        result = run_call(
            self.case,
            "latest",
            1,
            self.repo,
            self.root / "bad",
            FixtureAdapter((ScriptedResult(response="not json"),)),
            5,
        )
        self.assertEqual(result["outcome"], "response_rejected")
        self.assertTrue(result["workspace_unchanged"])

    def test_workspace_mutation_is_reported_even_with_valid_output(self):
        def mutate(*_args):
            (self.repo / "feature.py").write_text("changed\n")
            return self.review

        with mock.patch(
            "experiments.review_context.validate_review", side_effect=mutate
        ):
            result = run_call(
                self.case,
                "full",
                1,
                self.repo,
                self.root / "mutated",
                FixtureAdapter((ScriptedResult(response=json.dumps(self.review)),)),
                5,
            )
        self.assertEqual(result["outcome"], "succeeded")
        self.assertFalse(result["workspace_unchanged"])

    def test_directory_symlink_changes_are_included_in_workspace_check(self):
        (self.repo / "one").mkdir()
        (self.repo / "two").mkdir()
        link = self.repo / "alias"
        link.symlink_to("one", target_is_directory=True)
        before = workspace_state(self.repo)
        link.unlink()
        link.symlink_to("two", target_is_directory=True)
        self.assertNotEqual(workspace_state(self.repo), before)
