import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_export import ExportError, read_bytes, receipt_bound_inference_artifacts
from afk_inference import Capability, FixtureAdapter, InferenceRuntime, ScriptedResult
from afk_metrics.__main__ import _human
from afk_metrics.report import (
    MAX_JSONL_RECORD_BYTES,
    MAX_REPORTED_IDENTITIES,
    _invocation,
    _seconds,
    _union_seconds,
    _validate_pi_metric_receipt,
    _validated_publication,
    _validator_seconds,
    _verify_generic_receipt,
    build_report,
    parse_pi_events,
    summarize_source,
)
from tests import test_export_cli

ROOT = Path(__file__).parents[1]


class MetricsEventTests(unittest.TestCase):
    def test_partial_token_categories_do_not_claim_complete_coverage(self):
        for kind in ("message_end", "compaction_end"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "events.jsonl"
                event = (
                    {
                        "type": kind,
                        "message": {"role": "assistant", "usage": {"input": 100}},
                    }
                    if kind == "message_end"
                    else {"type": kind, "result": {"usage": {"input": 100}}}
                )
                path.write_text(json.dumps(event) + "\n")
                self.assertEqual(parse_pi_events(path)["coverage"], "partial")

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

    def test_compaction_without_measurements_keeps_coverage_partial(self):
        events = [
            {
                "type": "message_end",
                "message": {
                    "id": "measured",
                    "role": "assistant",
                    "usage": {"input": 4, "cost": {"total": 0.01}},
                },
            },
            {"type": "compaction_end", "id": "unmeasured", "result": {}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["compaction"]["aggregate_count"], 1)
        self.assertEqual(result["compaction"]["usage"], {})
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["cost"]["status"], "partial")

    def test_retry_attempt_numbers_are_scoped_to_each_retry_episode(self):
        events = [
            {"type": "auto_retry_start", "attempt": 1},
            {"type": "auto_retry_end", "attempt": 1, "success": True},
            {"type": "auto_retry_start", "attempt": 1},
            {"type": "auto_retry_end", "attempt": 1, "success": True},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["retry_count"], 2)

    def test_reported_identities_are_bounded_for_large_unique_streams(self):
        events = [
            {
                "type": "message_end",
                "message": {
                    "id": f"message-{index}",
                    "role": "assistant",
                    "provider": "provider",
                    "model": f"model-{index}",
                    "usage": {"input": 1},
                },
            }
            for index in range(MAX_REPORTED_IDENTITIES + 20)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(len(result["identities"]), MAX_REPORTED_IDENTITIES)
        self.assertEqual(result["identity_coverage"], "partial")
        self.assertEqual(result["usage"]["input"], len(events))

    def test_retry_makes_cost_coverage_partial_even_with_a_measured_final(self):
        events = [
            {"type": "auto_retry_start", "attempt": 1},
            {
                "type": "message_end",
                "message": {
                    "id": "ok",
                    "role": "assistant",
                    "usage": {"input": 1, "cost": {"total": 0.01}},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = parse_pi_events(path)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["cost"]["status"], "partial")

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

    def test_identity_fields_cannot_carry_arbitrary_sensitive_content(self):
        events = [
            {
                "type": "message_end",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "provider": "TOP SECRET prompt content",
                    "model": "sk-live-credential",
                    "usage": {"input": 1},
                },
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(json.dumps(events[0]) + "\n")
            result = parse_pi_events(path)
        self.assertEqual(result["identities"], [])
        self.assertNotIn("TOP SECRET", json.dumps(result))
        self.assertNotIn("sk-live", json.dumps(result))

    def test_descriptor_stream_must_match_authenticated_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(json.dumps({"type": "agent_end"}) + "\n")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with self.assertRaisesRegex(ValueError, "hash disagrees"):
                    parse_pi_events(descriptor, "0" * 64)
            finally:
                os.close(descriptor)

    def test_stream_rejects_an_oversized_record_at_a_fixed_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            with path.open("wb") as stream:
                stream.write(b'{"type":"event","padding":"')
                stream.write(b"x" * MAX_JSONL_RECORD_BYTES)
                stream.write(b'"}\n')
            with self.assertRaisesRegex(ValueError, "oversized JSONL event at line 1"):
                parse_pi_events(path)


class MetricsIntegrityTests(unittest.TestCase):
    def test_export_schema_dereferences_fail_as_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inference").mkdir()
            with (
                mock.patch(
                    "afk_export._receipt_bound_inference_artifacts",
                    side_effect=KeyError("duration_seconds"),
                ),
                self.assertRaisesRegex(ExportError, "invalid Inference Receipt"),
            ):
                receipt_bound_inference_artifacts(
                    root, "inference", "feedback_response"
                )

    def test_pi_metric_receipt_rejects_malformed_trusted_fields(self):
        receipt = {
            "policy": {"max_attempts": 1},
            "timing": {
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1,
                "timeout_seconds": 2,
            },
            "attempt_count": 0,
            "attempts": [],
            "protocol": {"status": "not_started"},
            "validation": {"status": "not_run"},
            "outcome": "adapter_failed",
        }
        invocation = {"timeout_seconds": 2}
        _validate_pi_metric_receipt(receipt, invocation)
        receipt["outcome"] = {"untrusted": "content"}
        with self.assertRaisesRegex(ValueError, "outcome or timing"):
            _validate_pi_metric_receipt(receipt, invocation)

    def test_pi_metric_receipt_binds_timeout_to_invocation(self):
        receipt = {
            "policy": {"max_attempts": 1},
            "timing": {
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1,
                "timeout_seconds": 2,
            },
            "attempt_count": 0,
            "attempts": [],
            "protocol": {"status": "not_started"},
            "validation": {"status": "not_run"},
            "outcome": "adapter_failed",
        }
        with self.assertRaisesRegex(ValueError, "outcome or timing"):
            _validate_pi_metric_receipt(receipt, {"timeout_seconds": 3})
        receipt["timing"]["timeout_seconds"] = 0
        with self.assertRaisesRegex(ValueError, "outcome or timing"):
            _validate_pi_metric_receipt(receipt, {"timeout_seconds": 0})

    def test_pi_validator_time_cannot_exceed_invocation_elapsed(self):
        receipt = {
            "policy": {"max_attempts": 1},
            "timing": {
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1,
                "timeout_seconds": 2,
            },
            "attempt_count": 1,
            "attempts": [
                {
                    "attempt_number": 1,
                    "duration_seconds": 1,
                    "protocol": {"status": "accepted"},
                    "validation": {
                        "status": "accepted",
                        "attempt_number": 1,
                        "validator_duration_seconds": 100,
                    },
                }
            ],
            "protocol": {"status": "accepted"},
            "validation": {"status": "accepted"},
            "outcome": "succeeded",
        }
        with self.assertRaisesRegex(ValueError, "exceeds invocation"):
            _validate_pi_metric_receipt(receipt, {"timeout_seconds": 2})

    def test_zero_validator_duration_is_available(self):
        attempts = [
            {
                "validation": {
                    "status": "accepted",
                    "attempt_number": 1,
                    "validator_duration_seconds": 0,
                }
            }
        ]
        self.assertEqual(_validator_seconds(attempts), 0)

    def test_unsupported_adapter_is_preserved_with_unavailable_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "worker/inference"
            evidence.mkdir(parents=True)
            invocation = {
                "schema_version": 1,
                "purpose": "feedback_response",
                "adapter": {"kind": "copilot", "identity": "copilot-v1"},
            }
            raw = json.dumps(invocation).encode()
            (evidence / "invocation.json").write_bytes(raw)
            receipt = {
                "schema_version": 1,
                "identity": {
                    "runtime": "afk-inference-v1",
                    "adapter": "copilot-v1",
                    "adapter_family": "copilot",
                },
                "hashes": {"invocation_sha256": hashlib.sha256(raw).hexdigest()},
                "timing": {
                    "started_at": "2026-01-01T00:00:00Z",
                    "ended_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1,
                },
                "outcome": "succeeded",
                "attempt_count": 2,
                "attempts": [
                    {
                        "attempt_number": 1,
                        "duration_seconds": 0.4,
                        "validation": {"validator_duration_seconds": "opaque"},
                    },
                    {"attempt_number": 2, "duration_seconds": 0.6},
                ],
            }
            (evidence / "receipt.json").write_text(json.dumps(receipt))
            result = _invocation(root, "worker/inference", "feedback_response")
            receipt["outcome"] = "prompt text must not become an outcome"
            (evidence / "receipt.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "identity disagrees"):
                _invocation(root, "worker/inference", "feedback_response")
        self.assertEqual(result["adapter_family"], "copilot")
        self.assertEqual(result["metrics"]["coverage"], "unavailable")
        self.assertEqual(result["metrics"]["reason"], "unsupported_adapter")
        self.assertEqual(result["metrics"]["retry_count"], 1)
        self.assertIsNone(result["response_validator_seconds"])
        self.assertEqual(result["response_validator_coverage"], "unavailable")

    def test_same_size_in_place_mutation_fails_descriptor_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            path.write_bytes(b"aaaa")
            original_read = os.read
            changed = False

            def mutate_after_read(descriptor, count):
                nonlocal changed
                data = original_read(descriptor, count)
                if data and not changed:
                    changed = True
                    path.write_bytes(b"bbbb")
                return data

            with (
                mock.patch("afk_export.os.read", side_effect=mutate_after_read),
                self.assertRaisesRegex(ExportError, "changed while being read"),
            ):
                read_bytes(path, 100)

    def test_pi_rejects_impossible_multiple_runtime_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "inference"
            evidence.mkdir()
            streams = [
                [{"type": "agent_end"}],
                [
                    {"type": "auto_retry_start", "attempt": 1},
                    {
                        "type": "message_end",
                        "message": {
                            "id": "final",
                            "role": "assistant",
                            "usage": {"input": 1},
                        },
                    },
                ],
            ]
            attempts = []
            for index, events in enumerate(streams, 1):
                raw = "".join(json.dumps(event) + "\n" for event in events).encode()
                name = f"events-{index}.jsonl"
                (evidence / name).write_bytes(raw)
                attempts.append(
                    {
                        "attempt_number": index,
                        "duration_seconds": 1,
                        "protocol": {
                            "status": "adapter_failed" if index == 1 else "accepted"
                        },
                        "artifacts": {
                            "events": name,
                            "events_sha256": hashlib.sha256(raw).hexdigest(),
                        },
                    }
                )
            receipt = {
                "policy": {"max_attempts": 1},
                "identity": {"adapter_family": "pi", "adapter": "pi-v1"},
                "hashes": {"invocation_sha256": "a" * 64},
                "timing": {
                    "started_at": "2026-01-01T00:00:00Z",
                    "ended_at": "2026-01-01T00:00:02Z",
                    "duration_seconds": 2,
                    "timeout_seconds": 10,
                },
                "attempt_count": 2,
                "attempts": attempts,
                "protocol": {"status": "accepted"},
                "validation": {"status": "accepted"},
                "outcome": "succeeded",
            }
            invocation = {"timeout_seconds": 10}

            def consume_bound(
                _root, _relative, _purpose, authenticated_context_consumer=None
            ):
                descriptor = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    consumed = authenticated_context_consumer(
                        descriptor, receipt, invocation
                    )
                finally:
                    os.close(descriptor)
                return {}, consumed

            with (
                mock.patch(
                    "afk_metrics.report._safe_evidence_json", return_value=receipt
                ),
                mock.patch(
                    "afk_metrics.report.receipt_bound_inference_artifacts",
                    side_effect=consume_bound,
                ),
                self.assertRaisesRegex(ValueError, "Pi receipt"),
            ):
                _invocation(root, "inference", "feedback_response")

    def test_generic_receipt_binds_script_identity_policy_timing_and_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            InferenceRuntime().invoke(
                purpose="classify",
                trusted_task_instructions="Return the value.",
                untrusted_task_data={"value": "ok"},
                requested_capability=Capability.NO_TOOLS,
                execution_root=root,
                timeout_seconds=1,
                evidence_directory=root / "evidence",
                validator=lambda value: value,
                adapter=FixtureAdapter((ScriptedResult(response="ok"),)),
            )
            receipt = json.loads((root / "evidence/receipt.json").read_text())
            _verify_generic_receipt(root / "evidence", receipt)
            changed = json.loads(json.dumps(receipt))
            changed["policy"]["requested_capability"] = "write"
            with self.assertRaisesRegex(ValueError, "identity or policy"):
                _verify_generic_receipt(root / "evidence", changed)
            changed = json.loads(json.dumps(receipt))
            changed["attempt_count"] = 0
            with self.assertRaisesRegex(TypeError, "attempts"):
                _verify_generic_receipt(root / "evidence", changed)
            changed = json.loads(json.dumps(receipt))
            changed["hashes"].pop("adapter_script_sha256")
            with self.assertRaisesRegex(ValueError, "hash catalog"):
                _verify_generic_receipt(root / "evidence", changed)

    def test_publication_protocol_relationships_are_strict(self):
        publication = {
            "schema_version": 1,
            "status": "succeeded",
            "admission_outcome": "accepted",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "process": {"exit_code": 0},
            "error_category": None,
        }
        self.assertIs(_validated_publication(publication), publication)
        fabricated = {**publication, "admission_outcome": "invented"}
        with self.assertRaises(ValueError):
            _validated_publication(fabricated)
        contradictory = {**publication, "status": "failed"}
        with self.assertRaises(ValueError):
            _validated_publication(contradictory)
        reversed_timing = {
            **publication,
            "started_at": "2026-01-01T00:00:02Z",
            "finished_at": "2026-01-01T00:00:01Z",
        }
        with self.assertRaises(ValueError):
            _validated_publication(reversed_timing)


class MetricsReportTests(unittest.TestCase):
    def test_ambiguous_timezone_intervals_are_unavailable_without_crashing(self):
        naive = ("2026-01-01T00:00:00", "2026-01-01T00:00:01")
        aware = ("2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z")
        self.assertIsNone(_seconds(*naive))
        self.assertEqual(_union_seconds([naive, aware]), 2)

    def test_reversed_timestamps_are_invalid_not_zero_duration(self):
        self.assertIsNone(_seconds("2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"))

    def test_overlapping_active_intervals_are_attributed_once(self):
        intervals = [
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z"),
            ("2026-01-01T00:00:02Z", "2026-01-01T00:00:07Z"),
            ("2026-01-01T00:00:06Z", "2026-01-01T00:00:09Z"),
        ]
        self.assertEqual(_union_seconds(intervals), 9)

    def test_publication_is_not_counted_as_unattributed_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {"history": [], "status": "completed"},
                "coordinator": root,
                "preparation": {
                    "timestamps": {
                        "started_at": "2026-01-01T00:00:00Z",
                        "prepared_at": "2026-01-01T00:00:01Z",
                        "finished_at": "2026-01-01T00:00:08Z",
                    },
                    "repository": {},
                },
                "terminal_directory": root,
                "output": {"outcome": "completed", "decision": "exhausted"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            publication_path = root / "publication.json"
            publication_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "succeeded",
                        "admission_outcome": "accepted",
                        "started_at": "2026-01-01T00:00:08Z",
                        "finished_at": "2026-01-01T00:00:10Z",
                        "process": {"exit_code": 0},
                        "error_category": None,
                    }
                )
            )
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
                publication_path.unlink()
                publication_path.symlink_to(root / "forged-publication.json")
                invalid = summarize_source(root)
        self.assertEqual(report["outcome"]["completion_acceptance"], "unavailable")
        self.assertEqual(report["outcome"]["integration_status"], "unavailable")
        self.assertEqual(report["outcome"]["coordinator_decision"], "exhausted")
        self.assertEqual(report["timing"]["publication_seconds"], 2)
        self.assertEqual(report["timing"]["unattributed_seconds"], 7)
        self.assertIn(
            {"kind": "publication", "seconds": 2, "nonoverlapping_seconds": 2},
            report["timing"]["active_execution_intervals"],
        )
        self.assertEqual(invalid["integrity"]["status"], "invalid")

    def test_publication_read_does_not_have_a_path_check_read_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {"history": [], "status": "completed"},
                "coordinator": root,
                "preparation": {
                    "timestamps": {
                        "started_at": "2026-01-01T00:00:00Z",
                        "prepared_at": "2026-01-01T00:00:01Z",
                        "finished_at": "2026-01-01T00:00:08Z",
                    },
                    "repository": {},
                },
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            publication_path = root / "publication.json"
            publication_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "succeeded",
                        "admission_outcome": "accepted",
                        "started_at": "2026-01-01T00:00:08Z",
                        "finished_at": "2026-01-01T00:00:10Z",
                        "process": {"exit_code": 0},
                        "error_category": None,
                    }
                )
            )
            original_is_file = Path.is_file

            def replace_after_check(path):
                if path == publication_path:
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "status": "failed",
                                "admission_outcome": "rejected",
                                "started_at": "2026-01-01T00:00:08Z",
                                "finished_at": "2026-01-01T00:00:11Z",
                                "process": {"exit_code": 1},
                                "error_category": "admission_rejected",
                            }
                        )
                    )
                    return True
                return original_is_file(path)

            with (
                mock.patch("afk_metrics.report.load_source", return_value=observed),
                mock.patch.object(Path, "is_file", new=replace_after_check),
            ):
                report = summarize_source(root)
        self.assertEqual(report["integrity"]["status"], "verified")
        self.assertEqual(report["outcome"]["completion_acceptance"], "unavailable")
        self.assertEqual(report["outcome"]["integration_status"], "unavailable")

    def test_unknown_bases_cannot_establish_equivalent_conditions(self):
        rows = [
            {
                "source_identity": identity,
                "work": {
                    "objective_sha256": "objective",
                    "base_commit": None,
                    "validation_conditions_sha256": "validation",
                },
            }
            for identity in ("a", "b")
        ]
        with mock.patch("afk_metrics.report.summarize_source", side_effect=rows):
            comparison = build_report([Path("a"), Path("b")])["comparisons"][0]
        self.assertFalse(comparison["equivalent_frozen_conditions"])
        self.assertIn("unavailable base code state", comparison["warnings"])

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
            # One abandoned invocation was sealed; the other is only an
            # interrupted directory prefix and must remain partial/unavailable.
            (coordinator / "02-response/inference/receipt.json").write_text("{}")
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
                    "response_validator_seconds": 0,
                    "metrics": {
                        "retry_count": 0,
                        "coverage": "complete",
                        "usage": {"input": 1},
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

            def missing_usage(_root, relative, purpose):
                value = invocation(_root, relative, purpose)
                value["metrics"]["coverage"] = "partial"
                value["metrics"]["usage"] = {}
                return value

            with (
                mock.patch("afk_metrics.report.load_source", return_value=observed),
                mock.patch("afk_metrics.report._invocation", side_effect=missing_usage),
            ):
                missing_report = summarize_source(root)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len(report["inference"]["invocations"]), 2)
        self.assertEqual(
            report["inference"]["invocations"][0]["metrics"]["reason"],
            "unsealed_abandoned_invocation",
        )
        self.assertIsNone(report["inference"]["totals"]["elapsed_seconds"])
        self.assertEqual(report["timing"]["response_validator_seconds"], 0)
        self.assertEqual(report["timing"]["response_validator_coverage"], "partial")
        self.assertEqual(
            report["timing"]["repository_validation_coverage"], "unavailable"
        )
        self.assertEqual(report["inference"]["totals"]["usage"], {"input": 1})
        self.assertEqual(report["inference"]["totals"]["usage_coverage"], "partial")
        self.assertEqual(
            missing_report["inference"]["totals"]["usage_coverage"], "unavailable"
        )
        # The synthetic invocation has no authenticated endpoints, so its
        # duration cannot safely be subtracted from wall time.
        self.assertEqual(report["timing"]["unattributed_seconds"], 10)

    def test_validation_completion_extends_continuation_wall_span(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "01-validation").mkdir()
            output = {
                "outcome": "passed",
                "duration_seconds": 3,
                "started_at": "2026-01-01T00:00:09Z",
                "finished_at": "2026-01-01T00:00:12Z",
            }
            observed = {
                "identity": {"run_id": "run-1.continuation.01"},
                "assignment": {"objective": "objective"},
                "state": {
                    "history": [
                        {
                            "component": "validation",
                            "directory": "01-validation",
                            "outcome": "passed",
                        }
                    ],
                    "status": "completed",
                },
                "coordinator": root,
                "preparation": {
                    "timestamps": {
                        "started_at": "2026-01-01T00:00:00Z",
                        "prepared_at": "2026-01-01T00:00:02Z",
                        "finished_at": "2026-01-01T00:00:05Z",
                    },
                    "repository": {},
                },
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with (
                mock.patch("afk_metrics.report.load_source", return_value=observed),
                mock.patch(
                    "afk_metrics.report._safe_evidence_json", return_value=output
                ),
                mock.patch(
                    "afk_metrics.report.validate_component_output",
                    return_value="passed",
                ),
                mock.patch("afk_metrics.report.normalize_component_output"),
            ):
                report = summarize_source(root)
        self.assertEqual(report["timing"]["run_wall_span_seconds"], 12)
        self.assertEqual(report["timing"]["unattributed_seconds"], 7)

    def test_validation_component_symlink_is_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = root / "external"
            external.mkdir()
            (external / "output.json").write_text("{}")
            os.symlink(external, root / "01-validation")
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {
                    "history": [
                        {
                            "component": "validation",
                            "directory": "01-validation",
                            "outcome": "passed",
                        }
                    ],
                    "status": "completed",
                },
                "coordinator": root,
                "preparation": {"timestamps": {}, "repository": {}},
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
        self.assertEqual(report["integrity"]["status"], "invalid")
        self.assertIsNone(report["timing"])

    def test_malformed_validation_output_is_invalid_not_a_metric(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validation = root / "01-validation"
            validation.mkdir()
            (validation / "output.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "passed",
                        "duration_seconds": "forged",
                    }
                )
            )
            history = [
                {
                    "component": "validation",
                    "directory": "01-validation",
                    "outcome": "passed",
                }
            ]
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {"history": history, "status": "completed"},
                "coordinator": root,
                "preparation": {"timestamps": {}, "repository": {}},
                "terminal_directory": validation,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
        self.assertEqual(report["integrity"]["status"], "invalid")
        self.assertIsNone(report["timing"])

    def test_preparation_records_expected_acceptance_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {"history": [], "status": "completed"},
                "coordinator": root,
                "preparation": {
                    "routing": {"planner": {"status": "completed"}},
                    "timestamps": {},
                    "repository": {},
                },
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
        self.assertEqual(
            report["inference"]["evidence_coverage"],
            {
                "status": "unavailable",
                "expected": 1,
                "measured": 0,
                "missing": [
                    {
                        "ownership": {
                            "kind": "run",
                            "purpose": "acceptance_planning",
                        },
                        "reason": "missing_receipt",
                    }
                ],
            },
        )

    def test_continuation_alias_rejects_a_distinct_authenticated_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordinator = root / "coordinator"
            continuation = root / "continuations/01"
            original = coordinator / "01-review/inference"
            retained = continuation / "01-review/inference"
            original.parent.mkdir(parents=True)
            retained.parent.mkdir(parents=True)
            helper = test_export_cli.ExportCliTests()
            helper.add_inference_receipt(original)
            helper.add_inference_receipt(retained)
            observed = {
                "identity": {"run_id": "run-1.continuation.01"},
                "assignment": {"objective": "objective"},
                "state": {
                    "history": [
                        {
                            "sequence": 1,
                            "component": "review",
                            "directory": "01-review",
                            "outcome": "completed",
                        }
                    ],
                    "status": "completed",
                },
                "coordinator": coordinator,
                "continuations": [continuation],
                "preparation": {"timestamps": {}, "repository": {}},
                "terminal_directory": continuation,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
        self.assertEqual(report["integrity"]["status"], "invalid")
        self.assertIsNone(report["inference"])

    def test_verified_no_action_response_is_not_expected_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            response = root / "01-response"
            response.mkdir()
            (response / "output.json").write_text(
                json.dumps(
                    {
                        "outcome": "completed",
                        "process": None,
                        "agent": None,
                        "response": {"finding_responses": [], "summary": "No action."},
                        "repository": {"unchanged": True},
                    }
                )
            )
            observed = {
                "identity": {"run_id": "run-1"},
                "assignment": {"objective": "objective"},
                "state": {
                    "history": [
                        {
                            "sequence": 1,
                            "component": "response",
                            "directory": "01-response",
                            "outcome": "completed",
                        }
                    ],
                    "status": "completed",
                },
                "coordinator": root,
                "preparation": {"timestamps": {}, "repository": {}},
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            with mock.patch("afk_metrics.report.load_source", return_value=observed):
                report = summarize_source(root)
        self.assertEqual(
            report["inference"]["evidence_coverage"],
            {"status": "complete", "expected": 0, "measured": 0, "missing": []},
        )
        self.assertEqual(report["inference"]["invocations"], [])

    def test_continuation_invocation_extends_original_run_wall_span(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = {
                "identity": {"run_id": "run-1.continuation.01"},
                "assignment": {"objective": "objective"},
                "state": {"history": [], "status": "completed"},
                "coordinator": root,
                "preparation": {
                    "timestamps": {
                        "started_at": "2026-01-01T00:00:00Z",
                        "prepared_at": "2026-01-01T00:00:01Z",
                        "finished_at": "2026-01-01T00:00:05Z",
                    },
                    "repository": {},
                    "routing": {"planner": {"status": "completed"}},
                },
                "terminal_directory": root,
                "output": {"outcome": "completed"},
                "request": {"validation": {}},
                "bead_id": None,
            }
            (root / "planner/inference").mkdir(parents=True)
            (root / "planner/inference/receipt.json").write_text("{}")
            invocation = {
                "source_event_identity": "continued-event",
                "elapsed": {
                    "seconds": 2,
                    "started_at": "2026-01-01T00:00:18Z",
                    "ended_at": "2026-01-01T00:00:20Z",
                },
                "response_validator_seconds": None,
                "metrics": {
                    "retry_count": 0,
                    "coverage": "complete",
                    "usage": {"input": 1},
                    "compaction": {"usage": {}},
                    "cost": {"amount": None, "status": "unavailable"},
                },
            }
            with (
                mock.patch("afk_metrics.report.load_source", return_value=observed),
                mock.patch("afk_metrics.report._invocation", return_value=invocation),
            ):
                report = summarize_source(root)
        self.assertEqual(report["timing"]["run_wall_span_seconds"], 20)
        self.assertEqual(report["timing"]["unattributed_seconds"], 17)

    def test_divergent_sources_with_one_identity_fail_closed(self):
        first = {
            "source_identity": "stable",
            "integrity": {"status": "verified"},
            "run_identity": {"run_id": "same"},
            "work": {
                "objective_sha256": "a",
                "base_commit": "b",
                "validation_conditions_sha256": "c",
            },
            "outcome": {"terminal": "completed"},
            "inference": {"totals": {"usage": {"input": 1}}},
            "timing": {},
        }
        second = json.loads(json.dumps(first))
        second["outcome"]["terminal"] = "failed"
        with mock.patch(
            "afk_metrics.report.summarize_source", side_effect=[first, second, first]
        ):
            report = build_report([Path("one"), Path("two"), Path("one")])
        self.assertEqual(len(report["runs"]), 1)
        conflict = report["runs"][0]
        self.assertEqual(conflict["integrity"]["error"], "ConflictingSourceEvidence")
        self.assertEqual(conflict["integrity"]["variant_count"], 2)
        self.assertIsNone(conflict["inference"])

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
                            "compaction_usage": {"input": 3},
                            "usage_coverage": "partial",
                            "cost": {"amount": 0.01, "status": "partial"},
                        },
                    },
                    "timing": {
                        "run_wall_span_seconds": 3,
                        "response_validator_seconds": 0.25,
                        "response_validator_coverage": "partial",
                        "repository_validation_seconds": 1,
                        "repository_validation_coverage": "partial",
                    },
                }
            ],
            "comparisons": [],
        }
        human = _human(report)
        self.assertIn("provider=openai", human)
        self.assertIn("model=gpt-test", human)
        self.assertIn('usage (partial coverage): {"input": 2}', human)
        self.assertIn(
            'compaction usage (separate aggregate; partial coverage): {"input": 3}',
            human,
        )
        self.assertIn("accepted / succeeded", human)
        self.assertIn(
            "response validation: 0.25 s (coverage: partial; "
            "inference response validator, not repository testing)",
            human,
        )
        self.assertIn("repository Validation: 1 s (coverage: partial)", human)
        self.assertIn(
            "API cost: 0.01 (status: partial; Pi-reported estimate, "
            "not billed charges)",
            human,
        )


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
