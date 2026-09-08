import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_metrics.__main__ import _human
from afk_metrics.report import (
    MAX_JSONL_RECORD_BYTES,
    build_report,
    parse_pi_events,
    summarize_source,
)
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

    def test_cost_is_retained_when_token_categories_are_missing(self):
        events = [
            {
                "type": "message_end",
                "message": {
                    "id": "cost-only",
                    "role": "assistant",
                    "usage": {"cost": {"total": 0.125}},
                },
            },
            {
                "type": "compaction_end",
                "id": "compact-cost-only",
                "result": {"usage": {"cost": {"total": 0.25}}},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["cost"]["amount"], 0.125)
        self.assertEqual(result["compaction_cost"], 0.25)
        self.assertEqual(result["compaction"]["aggregate_count"], 1)
        self.assertEqual(result["usage"], {})

    def test_stream_rejects_malformed_json_without_exposing_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                '{"type":"message_end","prompt":"secret"}\nnot-json-secret\n'
            )
            with self.assertRaisesRegex(ValueError, "line 2") as caught:
                parse_pi_events(path)
            self.assertNotIn("secret", str(caught.exception))

    def test_stream_rejects_an_oversized_record_at_a_fixed_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            with path.open("wb") as stream:
                stream.write(b'{"type":"event","padding":"')
                stream.write(b"x" * MAX_JSONL_RECORD_BYTES)
                stream.write(b'"}\n')
            with self.assertRaisesRegex(ValueError, "oversized JSONL event at line 1"):
                parse_pi_events(path)


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

    def test_abandoned_invocations_are_retained_and_partial_timing_uses_known_values(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordinator = root / "coordinator"
            for name in ("01-attempt", "02-response"):
                (coordinator / name / "inference").mkdir(parents=True)
            history = [
                {
                    "component": "attempt",
                    "directory": "01-attempt",
                    "outcome": "abandoned",
                },
                {
                    "component": "response",
                    "directory": "02-response",
                    "outcome": "abandoned",
                },
            ]
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {"history": history, "status": "interrupted"},
                "coordinator": coordinator,
                "preparation": {
                    "timestamps": {
                        "started_at": "2026-01-01T00:00:00Z",
                        "prepared_at": "2026-01-01T00:00:00Z",
                        "finished_at": "2026-01-01T00:00:10Z",
                    },
                    "repository": {"base_commit": "a" * 40},
                },
                "terminal_directory": coordinator / "02-response",
                "output": {"outcome": "interrupted"},
                "request": {"validation": {}},
                "bead_id": "bead-1",
            }

            def invocation(_root, relative, purpose):
                seconds = None if relative.startswith("coordinator/01") else 2
                return {
                    "source_event_identity": relative,
                    "purpose": purpose,
                    "elapsed": {"seconds": seconds},
                    "response_validator_seconds": None,
                    "metrics": {
                        "retry_count": 0,
                        "usage": {},
                        "compaction": {"usage": {}},
                        "cost": {"amount": None, "status": "unavailable"},
                    },
                }

            with (
                mock.patch("afk_metrics.report.load_source", return_value=observed),
                mock.patch(
                    "afk_metrics.report._invocation", side_effect=invocation
                ) as invoke,
            ):
                report = summarize_source(root)
        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(report["inference"]["totals"]["elapsed_seconds"])
        self.assertEqual(report["timing"]["unattributed_seconds"], 8)

    def test_human_report_shows_available_model_and_acceptance_evidence(self):
        report = {
            "runs": [
                {
                    "source_identity": "source-hash",
                    "integrity": {"status": "verified"},
                    "run_identity": {"run_id": "run-1", "bead_id": "bead-1"},
                    "outcome": {
                        "terminal": "completed",
                        "validation_results": ["passed"],
                        "repair_count": 1,
                        "retry_count": 2,
                        "completion_acceptance": "accepted",
                        "integration_status": "succeeded",
                    },
                    "inference": {
                        "invocations": [
                            {
                                "adapter": "pi-v1",
                                "provider": "openai",
                                "model": "gpt-test",
                            }
                        ],
                        "totals": {
                            "elapsed_seconds": 1,
                            "usage": {"input": 2},
                            "cost": {"amount": 0.01},
                        },
                    },
                    "timing": {
                        "run_wall_span_seconds": 3,
                        "repository_validation_seconds": 1,
                    },
                }
            ],
            "comparisons": [],
        }
        human = _human(report)
        self.assertIn("provider=openai", human)
        self.assertIn("model=gpt-test", human)
        self.assertIn("accepted / succeeded", human)


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

    def test_cli_rejects_destination_nested_in_source_before_creating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            destination = source / "report"
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
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
