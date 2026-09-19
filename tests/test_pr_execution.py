import copy
import unittest

from afk_pr.execution import summarize

HEAD = "a" * 40
OLD = "b" * 40
JOB = "0123456789abcdef"


class ExecutionSummaryTests(unittest.TestCase):
    def context(self, statuses=(), reviews=(), checks=()):
        return {
            "observed_at": "2026-09-18T23:18:02Z",
            "pull_request": {
                "html_url": "https://github.com/example/repo/pull/61",
                "head": {"sha": HEAD},
                "base": {"sha": OLD},
            },
            "statuses": list(statuses),
            "reviews": list(reviews),
            "checks": list(checks),
        }

    def status(self, state, time, producer=1, job=JOB):
        return {
            "id": 1,
            "context": f"afk/fixtures/{job}",
            "state": state,
            "creator": {"id": producer, "login": f"user{producer}"},
            "updated_at": time,
            "target_url": "https://github.com/example/repo/pull/61",
        }

    def test_later_pass_is_visible_despite_stale_review_and_comment_prose(self):
        context = self.context(
            [
                self.status("pending", "2026-09-18T23:04:10Z"),
                self.status("success", "2026-09-18T23:06:49Z"),
            ],
            [
                {
                    "commit_id": HEAD,
                    "state": "COMMENT",
                    "submitted_at": "2026-09-18T23:06:25Z",
                    "body": "No successful current-head run exists.",
                }
            ],
        )
        context["comments"] = [{"body": "Everything failed"}]
        original = copy.deepcopy(context)
        result = summarize(context)
        self.assertEqual(context, original)
        self.assertEqual([s["state"] for s in result["statuses"]], ["success"])
        self.assertEqual(result["statuses"][0]["fixture_job_id"], JOB)
        self.assertTrue(result["reviews"][0]["current_head"])
        self.assertNotIn("body", result["reviews"][0])
        self.assertNotIn("ready", result)

    def test_distinct_runs_producers_and_unknown_inputs_do_not_become_one_pass(self):
        statuses = [
            self.status("success", "2026-09-18T23:06:49Z"),
            self.status("failure", "2026-09-18T23:07:00Z", producer=2),
            self.status("pending", "2026-09-18T23:08:00Z", job="f" * 16),
        ]
        result = summarize(self.context(statuses))
        self.assertEqual(len(result["statuses"]), 3)
        for item in result["statuses"]:
            self.assertIsNone(item["profile"])
            self.assertIsNone(item["input_identity"])

    def test_old_or_missing_revisions_cannot_verify_current_head(self):
        old_status = self.status("success", "2026-09-18T23:06:49Z")
        old_status["sha"] = OLD
        result = summarize(
            self.context(
                [old_status],
                [{"commit_id": OLD}, {}],
                [{"head_sha": OLD, "conclusion": "success"}, {}],
            )
        )
        self.assertFalse(result["statuses"][0]["current_head"])
        self.assertFalse(result["checks"][0]["current_head"])
        self.assertIsNone(result["checks"][1]["current_head"])
        self.assertFalse(result["reviews"][0]["current_head"])
        self.assertIsNone(result["reviews"][1]["current_head"])

    def test_check_reruns_are_retained_individually(self):
        checks = [
            {
                "id": 1,
                "name": "tests",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "id": 2,
                "name": "tests",
                "head_sha": HEAD,
                "status": "in_progress",
                "conclusion": None,
            },
        ]
        result = summarize(self.context(checks=checks))
        self.assertEqual([item["id"] for item in result["checks"]], [1, 2])
        self.assertEqual(result["checks"][1]["state"], "in_progress")
        self.assertIsNone(result["checks"][1]["conclusion"])

    def test_order_is_selected_by_timestamp_not_array_order(self):
        later = self.status("error", "2026-09-18T23:07:00Z")
        earlier = self.status("success", "2026-09-18T23:06:49Z")
        for values in ([later, earlier], [earlier, later]):
            self.assertEqual(
                summarize(self.context(values))["statuses"][0]["state"], "error"
            )

    def test_missing_provenance_and_empty_evidence_are_not_success(self):
        result = summarize(self.context([{"state": "pending"}, {"state": "error"}]))
        self.assertEqual(len(result["statuses"]), 2)
        self.assertEqual(summarize(self.context())["statuses"], [])
        self.assertTrue(result["limits"])


if __name__ == "__main__":
    unittest.main()
