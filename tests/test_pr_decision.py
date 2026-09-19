import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_pr import decision, jobs
from afk_pr.__main__ import main

URL = "https://github.com/example/repo/pull/1"
HEAD, BASE, OLD = "a" * 40, "b" * 40, "c" * 40
REVIEW, RESPONSE, FIXTURE = "1" * 16, "2" * 16, "3" * 16


def context():
    return {
        "observed_at": "2026-09-19T00:00:00Z",
        "pull_request": {
            "html_url": URL,
            "state": "open",
            "head": {"sha": HEAD},
            "base": {"sha": BASE},
        },
    }


def fixture():
    return {
        "state": "passed",
        "publication": "published",
        "candidate_unchanged": True,
        "process": {
            "exit_code": 0,
            "error": None,
            "timed_out": False,
            "interrupted": False,
        },
    }


def review():
    return {
        "job": {
            "id": REVIEW,
            "pr_url": URL,
            "head": HEAD,
            "base": BASE,
            "kind": "review",
            "expected_phases": ["fixtures", "review"],
        },
        "phases": {
            "fixtures": fixture(),
            "review": {
                "state": "completed",
                "publication": "published",
                "result": {
                    "schema_version": 1,
                    "head": HEAD,
                    "summary": "Inspected",
                    "findings": [],
                },
            },
        },
    }


def response():
    parent = {
        "job": {
            "id": RESPONSE,
            "pr_url": URL,
            "head": OLD,
            "base": BASE,
            "kind": "response",
            "expected_phases": ["response"],
        },
        "phases": {
            "response": {
                "state": "completed",
                "publication": "published",
                "progress": {
                    "changed": True,
                    "push": "pushed",
                    "candidate": HEAD,
                    "fixture_job": FIXTURE,
                },
            }
        },
    }
    child = {
        "job": {
            "id": FIXTURE,
            "pr_url": URL,
            "head": HEAD,
            "base": BASE,
            "kind": "review",
            "expected_phases": ["fixtures"],
            "response_job": RESPONSE,
        },
        "phases": {"fixtures": fixture()},
    }
    return parent, child


class DecisionTests(unittest.TestCase):
    def assert_decision(self, records, recommendation, code=None, observed=None):
        before = copy.deepcopy(records)
        result = decision.decide(observed or context(), records)
        self.assertEqual(result["recommendation"], recommendation, result)
        if code:
            self.assertIn(code, [item["code"] for item in result["reasons"]])
        self.assertEqual(records, before)
        return result

    def test_clear_or_actionable_review_allows_selection_not_merge(self):
        for findings in ([], [{"message": "Repair saturation behavior"}]):
            item = review()
            item["phases"]["review"]["result"]["findings"] = findings
            self.assert_decision([item], "continue", "selected_work_complete")

    def test_pending_work_waits_but_not_when_other_evidence_failed(self):
        item = review()
        item["phases"]["fixtures"] = {"state": "running", "publication": "pending"}
        self.assert_decision([item], "wait", "work_pending")
        item["phases"]["review"] = {"state": "failed", "publication": "published"}
        self.assert_decision([item], "pause", "execution_not_successful")

    def test_publication_retry_requires_successful_terminal_selected_work(self):
        item = review()
        item["phases"]["review"]["publication"] = "failed"
        result = self.assert_decision(
            [item], "retry_publication", "publication_incomplete"
        )
        self.assertEqual(result["publication_retry_job_ids"], [REVIEW])
        item["phases"]["fixtures"]["state"] = "running"
        self.assert_decision([item], "wait")
        item["phases"]["fixtures"]["state"] = "failed"
        self.assert_decision([item], "pause")

    def test_closed_pr_other_pr_and_old_head_or_base_pause(self):
        observed = context()
        observed["pull_request"]["state"] = "closed"
        self.assert_decision([review()], "pause", "pr_not_open", observed)
        for field, value, code in (
            ("head", OLD, "revision_changed"),
            ("base", OLD, "revision_changed"),
            ("pr_url", URL[:-1] + "2", "job_belongs_to_another_pr"),
        ):
            item = review()
            item["job"][field] = value
            self.assert_decision([item], "pause", code)
        item = review()
        item["job"]["pr_url"] = URL.replace("example/repo", "Example/Repo")
        self.assert_decision([item], "continue")

    def test_unknown_interrupted_missing_or_inconsistent_evidence_pauses(self):
        self.assert_decision([], "pause", "no_jobs_selected")
        self.assert_decision(
            [{"requested_id": REVIEW, "error": "missing"}],
            "pause",
            "evidence_unavailable",
        )
        for mutation, code in (
            (lambda x: x["phases"].pop("fixtures"), "phase_evidence_missing"),
            (lambda x: x["job"].pop("expected_phases"), "phase_selection_unknown"),
            (lambda x: x["phases"]["review"].pop("result"), "review_result_unknown"),
            (
                lambda x: x["phases"]["review"].update(state="interrupted"),
                "execution_not_successful",
            ),
            (
                lambda x: x["phases"]["review"].update(
                    worker_observation="unavailable"
                ),
                "worker_ownership_unknown",
            ),
            (
                lambda x: x["phases"]["fixtures"].update(candidate_unchanged=False),
                "fixture_result_inconsistent",
            ),
            (
                lambda x: x["phases"]["fixtures"]["process"].update(exit_code=1),
                "fixture_result_inconsistent",
            ),
            (
                lambda x: x["phases"]["fixtures"].update(state="timed_out"),
                "fixture_failed",
            ),
            (
                lambda x: x["phases"]["review"].update(publication="unknown"),
                "publication_state_unknown",
            ),
        ):
            item = review()
            mutation(item)
            self.assert_decision([item], "pause", code)

    def test_response_advancement_waits_for_linked_fixtures_then_continues(self):
        parent, child = response()
        child["phases"]["fixtures"] = {"state": "running", "publication": "pending"}
        self.assert_decision([parent, child], "wait")
        child["phases"]["fixtures"] = fixture()
        self.assert_decision([parent, child], "continue")
        self.assert_decision([parent], "pause", "fixture_child_missing_or_mismatched")
        child["job"]["response_job"] = REVIEW
        self.assert_decision(
            [parent, child], "pause", "fixture_child_missing_or_mismatched"
        )

    def test_uncertain_push_and_no_change_pause_but_active_push_waits(self):
        parent, child = response()
        parent["phases"]["response"]["progress"]["push"] = "attempted"
        self.assert_decision([parent, child], "pause", "push_unconfirmed")
        parent["phases"]["response"]["state"] = "running"
        self.assert_decision([parent, child], "wait")
        parent["phases"]["response"].update(state="completed")
        parent["phases"]["response"]["progress"].update(
            changed=False, candidate=OLD, push="not_started"
        )
        self.assert_decision([parent], "pause", "response_no_change")

    def test_action_receipt_must_match_and_be_settled(self):
        item = review()
        item["job"]["action_id"] = "review-1"
        self.assert_decision([item], "pause", "action_identity_unknown")
        item["action"] = {
            "schema_version": 1,
            "id": "review-1",
            "repository": "example/repo",
            "pr_number": 1,
            "job_id": REVIEW,
            "head": HEAD,
            "base": BASE,
            "command": "review",
            "state": "paused",
        }
        self.assert_decision([item], "pause", "action_unsettled")
        item["action"]["state"] = "submitted"
        self.assert_decision([item], "continue")
        item["action"]["job_id"] = RESPONSE
        self.assert_decision([item], "pause", "action_identity_unknown")

    def test_observation_reads_linked_children_and_changes_no_files(self):
        with tempfile.TemporaryDirectory() as root:
            for item in response():
                d = Path(root) / "pr-reviews" / item["job"]["id"]
                d.mkdir(parents=True)
                jobs.write(d / "job.json", item["job"])
                for phase, data in item["phases"].items():
                    data = copy.deepcopy(data)
                    if "progress" in data:
                        jobs.write(d / f"{phase}-progress.json", data.pop("progress"))
                    jobs.write(d / f"{phase}.json", data)

            def snapshot():
                return {
                    str(p): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()
                }

            before = snapshot()
            gh = mock.Mock()
            gh.observe.return_value = context()
            result = decision.observe(URL, root, [RESPONSE], github=gh)
            self.assertEqual(result["decision"]["recommendation"], "continue")
            self.assertEqual(len(result["jobs"]), 2)
            self.assertEqual(snapshot(), before)
            missing = decision.observe(URL, root, ["f" * 16], github=gh)
            self.assertEqual(missing["decision"]["recommendation"], "pause")
            with self.assertRaises(ValueError):
                decision.observe(URL, root, ["../escape"], github=gh)

    def test_cli_only_observes_and_returns_success_for_pause_advice(self):
        with (
            mock.patch(
                "afk_pr.config.load_config",
                return_value={"run_root": Path("/tmp/unused")},
            ),
            mock.patch.object(
                decision,
                "observe",
                return_value={"decision": {"recommendation": "pause"}},
            ) as observe,
            mock.patch("afk_pr.__main__.submit") as submit,
            mock.patch.object(jobs, "retry_publication") as retry,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(
                main(["status", URL, "--job", REVIEW, "--job", RESPONSE]), 0
            )
            self.assertEqual(observe.call_args.args[2], [REVIEW, RESPONSE])
            submit.assert_not_called()
            retry.assert_not_called()
