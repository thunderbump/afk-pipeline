"""Decision experiments for qsqc, entirely in memory."""

import unittest

from spikes.pr_finish import (
    SCENARIOS,
    FakeBeads,
    FakeGitHub,
    close_after_merge,
    preview,
    run_case,
)


class FinishSpikeTest(unittest.TestCase):
    def test_modes_have_same_external_result_and_preserve_parent(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                combined = run_case(scenario, "combined")
                separate = run_case(scenario, "separate")
                self.assertEqual(combined["beads"], separate["beads"])
                self.assertEqual(combined["beads"]["example-parent"], "open")
                self.assertEqual(
                    [r["merge"] for r in combined["results"]],
                    [r["merge"] for r in separate["results"]],
                )

    def test_changed_denied_and_closed_unmerged_never_close(self):
        for scenario, expected in (
            ("changed_head", "changed"),
            ("changed_base", "changed"),
            ("head_race", "changed"),
            ("denied", "rejected_or_unknown"),
            ("closed_unmerged", "closed_unmerged"),
        ):
            for mode in ("combined", "separate"):
                with self.subTest(scenario=scenario, mode=mode):
                    result = run_case(scenario, mode)
                    self.assertEqual(result["results"][0]["merge"], expected)
                    self.assertFalse(result["beads_calls"])
                    if scenario in {"changed_head", "changed_base", "closed_unmerged"}:
                        self.assertFalse(
                            any(c[0] == "merge" for c in result["github_calls"])
                        )

    def test_closure_retry_does_not_repeat_merge(self):
        for mode in ("combined", "separate"):
            result = run_case("closure_failure_retry", mode)
            self.assertEqual(
                [r["closure"] for r in result["results"]], ["failed", "closed"]
            )
            self.assertEqual(sum(c[0] == "merge" for c in result["github_calls"]), 1)
            self.assertEqual(result["beads"]["example-followup"], "closed")

    def test_already_merged_and_repeated_success_are_reconciled(self):
        for mode in ("combined", "separate"):
            result = run_case("already_merged", mode)
            self.assertEqual(result["results"][0]["closure"], "closed")
            self.assertFalse(any(c[0] == "merge" for c in result["github_calls"]))
            result = run_case("repeated_success", mode)
            self.assertEqual(result["results"][1]["closure"], "already_closed")
            self.assertEqual(sum(c[0] == "close" for c in result["beads_calls"]), 1)

    def test_queue_does_not_close_until_later_observation(self):
        for mode in ("combined", "separate"):
            result = run_case("queued_retry", mode)
            self.assertEqual(
                [r["merge"] for r in result["results"]], ["pending", "confirmed"]
            )
            self.assertNotIn(
                result["results"][0]["closure"], {"closed", "already_closed"}
            )
            self.assertEqual(result["results"][1]["closure"], "closed")
            self.assertEqual(sum(c[0] == "close" for c in result["beads_calls"]), 1)

    def test_lost_reply_reconciles_from_actual_merge(self):
        result = run_case("lost_merge_reply", "combined")
        self.assertEqual(result["results"][0]["merge"], "confirmed")
        self.assertEqual(result["results"][0]["closure"], "closed")

    def test_ambiguous_mapping_never_implicitly_closes(self):
        result = run_case("ambiguous_association", "combined")
        self.assertIsNone(result["preview"]["intent"]["close_bead"])
        self.assertEqual(len(result["preview"]["observed"]["associations"]), 2)
        self.assertEqual(result["results"][0]["closure"], "not_requested")
        self.assertFalse(result["beads_calls"])

    def test_standalone_closure_checks_current_merge_and_head(self):
        github, beads = FakeGitHub(), FakeBeads()
        intent, _ = preview(github, close_bead="example-followup")
        self.assertEqual(
            close_after_merge(intent, github, beads)["closure"], "not_confirmed"
        )
        github.merged = True
        github.head = "b" * 40
        self.assertEqual(
            close_after_merge(intent, github, beads)["closure"], "not_confirmed"
        )
        self.assertFalse(beads.calls)


if __name__ == "__main__":
    unittest.main()
