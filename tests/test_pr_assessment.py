import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from afk_inference import Capability, FixtureAdapter, ScriptedResult, invoke
from afk_pr import assessment
from afk_run import PreparationError, main

URL = "https://github.com/example/repo/pull/1"
SHA = "a" * 40


class AssessmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "host.toml"
        self.config.write_text(f'schema_version=1\nstate_root="{self.root}/state"\n')
        self.bead = {
            "id": "central-task",
            "title": "Task",
            "status": "open",
            "acceptance_criteria": "First requirement.\nSecond requirement.",
            "notes": "Deferred concern",
        }
        self.context = {
            "observed_at": "2026-09-16T00:00:00Z",
            "pull_request": {
                "body": "<!-- afk-bead:central-task -->",
                "head": {"sha": SHA},
                "base": {"sha": "b" * 40, "ref": "main"},
            },
            "comments": [{"body": "Third-party feedback", "created_at": "yesterday"}],
            "review_comments": [],
            "reviews": [{"commit_id": "c" * 40, "body": "Old review"}],
            "checks": [{"head_sha": SHA, "conclusion": "success"}],
            "statuses": [],
            "commits": [],
        }
        self.gh = Mock()
        self.gh.observe.return_value = self.context
        self.gh.api.return_value = self.context["pull_request"]
        self.calls = []

    def run_assessment(
        self,
        *,
        report="Insufficient evidence: the current head was not reviewed.",
        acquired=False,
        changed=False,
        **kwargs,
    ):
        def infer(**arguments):
            self.calls.append(arguments)
            return invoke(
                **arguments, adapter=FixtureAdapter((ScriptedResult(response=report),))
            )

        with (
            patch.object(assessment, "read_configured_bead", return_value=self.bead),
            patch.object(
                assessment.workspace,
                "acquire",
                return_value=self.root,
                side_effect=None
                if acquired
                else RuntimeError("acquisition unavailable"),
            ),
            patch.object(
                assessment.jobs, "git", side_effect=[SHA, "dirty" if changed else ""]
            ),
        ):
            return assessment.assess(
                URL, self.config, github=self.gh, inference=infer, **kwargs
            )

    def test_frozen_full_story_and_actual_bead_no_mutation(self):
        result = self.run_assessment(acquired=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["selection"], "pr_marker")
        self.assertEqual(result["freshness"], "unchanged")
        directory = Path(result["directory"])
        self.assertEqual(
            json.loads((directory / "context.json").read_text()), self.context
        )
        self.assertEqual(
            json.loads((directory / "bead.json").read_text())["acceptance_criteria"],
            self.bead["acceptance_criteria"],
        )
        self.assertTrue((directory / "inference/receipt.json").exists())
        call = self.calls[0]
        self.assertEqual(call["requested_capability"], Capability.READ_ONLY)
        self.assertEqual(len(call["read_only_evidence"]), 3)
        self.assertEqual(self.gh.api.call_args.args, ("repos/example/repo/pulls/1",))
        self.assertFalse(self.gh.api.call_args.kwargs)
        self.assertFalse((self.root / "state/pr-reviews").exists())

    def test_explicit_override_reads_selected_bead_not_marker(self):
        with (
            patch.object(
                assessment, "read_configured_bead", side_effect=PreparationError("stop")
            ) as read,
            self.assertRaises(PreparationError),
        ):
            assessment.assess(URL, self.config, bead_id="followup", github=self.gh)
        self.assertEqual(read.call_args.args[0], "followup")
        self.assertFalse((self.root / "state/assessments").exists())

    def test_ambiguous_or_missing_markers_need_override(self):
        for body in (
            "mentions central-task",
            "<!-- afk-bead:a --> <!-- afk-bead:b -->",
        ):
            self.context["pull_request"]["body"] = body
            with self.assertRaisesRegex(ValueError, "--bead"):
                self.run_assessment()
        result = self.run_assessment(bead_id="central-task")
        self.assertEqual(result["selection"], "explicit")

    def test_acquisition_failure_is_explicit_and_evidence_still_readable(self):
        result = self.run_assessment()
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["repository"]["available"])
        self.assertIn("unavailable", result["repository"]["unavailable_reason"])
        self.assertTrue(
            all(Path(p).is_file() for p in self.calls[0]["read_only_evidence"])
        )
        self.assertEqual(self.calls[0]["execution_root"].name, "empty-context")

    def test_moving_head_and_unknown_freshness_cannot_silently_look_current(self):
        self.gh.api.return_value = {
            "head": {"sha": "c" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
        }
        result = self.run_assessment()
        self.assertEqual(result["freshness"], "changed")
        self.assertTrue(result["report"].startswith("CAUTION:"))
        self.gh.api.side_effect = RuntimeError("unavailable")
        result = self.run_assessment()
        self.assertEqual(result["freshness"], "unknown")
        self.assertIn("frozen revision", result["report"])

    def test_changed_base_branch_marks_report_stale(self):
        self.gh.api.return_value = {
            "head": {"sha": SHA},
            "base": {"sha": "b" * 40, "ref": "release"},
        }
        self.assertEqual(self.run_assessment()["freshness"], "changed")

    def test_empty_response_or_modified_checkout_fails_without_report(self):
        for options in ({"report": ""}, {"acquired": True, "changed": True}):
            result = self.run_assessment(**options)
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("report", result)
            self.assertFalse((Path(result["directory"]) / "report.md").exists())

    def test_context_failure_does_not_launch_inference(self):
        self.gh.observe.side_effect = ValueError("PR changed while reading context")
        with self.assertRaisesRegex(ValueError, "PR changed"):
            self.run_assessment()
        self.assertFalse(self.calls)

    def test_cli_routes_override_and_reports_execution_failure(self):
        with (
            patch(
                "afk_pr.assessment.assess", return_value={"state": "completed"}
            ) as call,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["assess", URL, "--bead", "followup"]), 0)
            self.assertEqual(call.call_args.kwargs["bead_id"], "followup")
        with (
            patch("afk_pr.assessment.assess", return_value={"state": "failed"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["assess", URL]), 1)


if __name__ == "__main__":
    unittest.main()
