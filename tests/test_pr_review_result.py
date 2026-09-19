import json
import tempfile
import unittest
from pathlib import Path

from afk_pr import jobs, review_result


class ReviewResultTests(unittest.TestCase):
    def test_empty_findings_and_textual_json_are_explicit(self):
        report = {"summary": "Inspected the change.", "findings": []}
        self.assertEqual(review_result.validate(json.dumps(report)), report)
        self.assertIn("No actionable findings", review_result.render(report))

    def test_invalid_contract_is_rejected(self):
        invalid = [
            "No concerns",
            {},
            {"summary": "ok", "findings": None},
            {"summary": "", "findings": []},
            {"summary": "ok", "findings": [{}]},
            {"summary": "ok", "findings": [{"message": "x", "line": 1}]},
            {
                "summary": "ok",
                "findings": [{"message": "x", "path": "x", "line": True}],
            },
            {"summary": "ok", "findings": [], "head": "invented"},
            {"summary": "x" * 50001, "findings": []},
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                review_result.validate(value)

    def test_retained_results_preserve_head_and_exclude_other_prs_and_failures(self):
        with tempfile.TemporaryDirectory() as root:
            url = "https://github.com/example/repo/pull/1"
            for name, state, head, pr in [
                ("old", "completed", "a" * 40, url),
                ("failed", "failed", "b" * 40, url),
                ("other", "completed", "b" * 40, url[:-1] + "2"),
                ("legacy", "completed", "b" * 40, url),
            ]:
                d = Path(root) / "pr-reviews" / name
                d.mkdir(parents=True)
                jobs.write(d / "job.json", {"id": name, "head": head, "pr_url": pr})
                record = {
                    "state": state,
                    "reviewer": "afk",
                    "result": {
                        "schema_version": 1,
                        "head": head,
                        "summary": "ok",
                        "findings": [],
                    },
                }
                if name == "legacy":
                    del record["result"]
                jobs.write(d / "review.json", record)
            results = review_result.retained(
                root, "https://github.com/Example/Repo/pull/1", "b" * 40
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["result"]["head"], "a" * 40)
            self.assertFalse(results[0]["current_head"])
            self.assertEqual(results[0]["publication"], "pending")
