import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from afk_metrics.report import build_report, parse_pi_events
from tests import test_export_cli

ROOT = Path(__file__).parents[1]


class MetricsEventTests(unittest.TestCase):
    def test_finalized_usage_is_counted_once_and_snapshots_are_ignored(self):
        events = [
            {"type": "message_update", "message": {"id": "m1", "usage": {"input": 5}}},
            {
                "type": "message_end",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "usage": {
                        "input": 10,
                        "output": 3,
                        "cacheRead": 2,
                        "cacheWrite": 1,
                        "reasoning": 2,
                        "totalTokens": 13,
                        "cost": {"total": 0.02},
                    },
                },
            },
            {"type": "turn_end", "message": {"id": "m1", "usage": {"input": 10}}},
            {"type": "agent_end", "messages": [{"id": "m1", "usage": {"input": 10}}]},
            # A duplicated finalized event with the same upstream identity.
            {
                "type": "message_end",
                "message": {"id": "m1", "role": "assistant", "usage": {"input": 10}},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["finalized_requests"], 1)
        self.assertEqual(result["usage"]["input"], 10)
        self.assertEqual(result["usage"]["reasoning"], 2)
        self.assertEqual(result["usage"]["totalTokens"], 13)
        self.assertEqual(result["cost"]["amount"], 0.02)
        self.assertEqual(result["cost"]["kind"], "pi_reported_estimate")

    def test_retry_missing_usage_and_compaction_are_partial_and_separate(self):
        events = [
            {
                "type": "message_end",
                "message": {"id": "failed", "role": "assistant", "stopReason": "error"},
            },
            {"type": "auto_retry_start", "attempt": 1},
            {
                "type": "message_end",
                "message": {
                    "id": "ok",
                    "role": "assistant",
                    "usage": {"input": 4, "output": 2},
                },
            },
            {
                "type": "compaction_end",
                "result": {"usage": {"input": 20, "cacheRead": 5}},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["usage"]["input"], 4)
        self.assertEqual(result["compaction"]["usage"]["input"], 20)
        self.assertIsNone(result["cost"]["amount"])
        self.assertFalse(result["request_count_exact"])

    def test_stream_rejects_malformed_json_without_exposing_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                '{"type":"message_end","prompt":"secret"}\nnot-json-secret\n'
            )
            with self.assertRaisesRegex(ValueError, "line 2") as caught:
                parse_pi_events(path)
            self.assertNotIn("secret", str(caught.exception))


class MetricsReportTests(unittest.TestCase):
    def test_fixture_run_comparison_matches_and_flags_confounded_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = test_export_cli.ExportCliTests()
            first = fixtures.sealed_preparer(root / "first")
            second = fixtures.sealed_preparer(root / "second")

            matched = build_report([first, second, first])
            self.assertEqual(len(matched["runs"]), 2)
            self.assertTrue(matched["comparisons"][0]["equivalent_frozen_conditions"])
            self.assertEqual(matched["comparisons"][0]["ranking"], "observational_only")

            preparation_path = second / "preparation.json"
            preparation = json.loads(preparation_path.read_text())
            preparation["repository"]["base_commit"] = "b" * 40
            preparation_path.write_text(json.dumps(preparation))
            mismatched = build_report([first, second])
            comparison = mismatched["comparisons"][0]
            self.assertFalse(comparison["equivalent_frozen_conditions"])
            self.assertIn("different base code state", comparison["warnings"])
            self.assertEqual(comparison["ranking"], "not_provided")


class MetricsCliTests(unittest.TestCase):
    def test_cli_always_uses_separate_new_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "not-a-run"
            source.mkdir()
            destination = root / "report"
            result = subprocess.run(
                [
                    "python3",
                    "-m",
                    "afk_metrics",
                    "--destination",
                    str(destination),
                    str(source),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            summary = json.loads((destination / "summary.json").read_text())
            self.assertEqual(summary["runs"][0]["integrity"]["status"], "invalid")
            self.assertIsNone(summary["runs"][0]["inference"])
            self.assertNotIn(str(source), (destination / "comparison.txt").read_text())
            again = subprocess.run(
                [
                    "python3",
                    "-m",
                    "afk_metrics",
                    "--destination",
                    str(destination),
                    str(source),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(again.returncode, 2)


if __name__ == "__main__":
    unittest.main()
