import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from afk_evidence import RunValidationError, TrustedContext, read_run
from afk_evidence.access import (
    EvidenceAccessError,
    EvidenceReader,
    EvidenceUnavailable,
)
from afk_evidence.continuation import (
    continuation_directories,
    observe_lineage,
    validate_link,
)
from afk_evidence.iteration import read_object
from afk_evidence.snapshot import _verify_stage_provenance


class RunSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.assignment = {
            "schema_version": 1,
            "objective": "Observe a failed run.",
            "workspace": str(self.root / "workspace"),
            "command": ["false"],
            "timeout_seconds": 2,
        }
        self.request = {
            "schema_version": 1,
            "assignment_path": str(self.run / "assignment.json"),
            "validation": {"command": ["true"], "timeout_seconds": 2},
            "agent_timeout_seconds": 2,
            "max_responses": 0,
        }
        self.history = [
            {
                "sequence": 1,
                "component": "attempt",
                "directory": "01-attempt",
                "input_from": {"assignment": "assignment.json"},
                "outcome": "failed",
            }
        ]
        self.state = {
            "schema_version": 1,
            "status": "failed",
            "next_sequence": 2,
            "next_component": None,
            "active_invocation": None,
            "history": self.history,
            "terminal": {
                "failed_component": "attempt",
                "component_outcome": "failed",
                "exit_code": 1,
            },
        }
        self.output = {
            "schema_version": 1,
            "outcome": "failed",
            "failed_component": "attempt",
            "component_outcome": "failed",
            "exit_code": 1,
            "history": self.history,
        }
        self.write("assignment.json", self.assignment)
        self.write("input.json", self.request)
        self.write("state.json", self.state)
        self.write("output.json", self.output)
        (self.run / "01-attempt").mkdir()
        self.write("01-attempt/output.json", {"schema_version": 1, "outcome": "failed"})

    def write(self, relative, value):
        (self.run / relative).write_text(json.dumps(value))

    def test_recorded_paths_cannot_expand_caller_authority(self):
        outside = self.root / "outside"
        outside.mkdir()
        reader = EvidenceReader((self.run,))

        with self.assertRaises(EvidenceAccessError):
            reader.authorize_directory(outside)

        missing = self.run / "retained" / "missing.json"
        reader.authorize_directory(missing.parent)
        with self.assertRaises(EvidenceUnavailable):
            reader.json(missing)

    def test_iteration_transitive_read_preserves_unavailable_identity(self):
        reader = EvidenceReader((self.run,))
        missing = self.run / "assessment" / "input.json"

        with self.assertRaises(EvidenceUnavailable) as caught:
            read_object(reader, missing, "Finding Assessment input")

        self.assertEqual(caught.exception.reason, "missing evidence")
        self.assertEqual(caught.exception.identity, str(missing))

    def test_reader_rejects_replacement_between_reads_in_one_snapshot(self):
        reader = EvidenceReader((self.run,))
        path = self.run / "state.json"
        self.assertEqual(reader.json(path), self.state)
        replacement = self.run / "replacement.json"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)

        with self.assertRaisesRegex(EvidenceAccessError, "between reads"):
            reader.json(path)

    def test_reader_rejects_metadata_changes_between_reads(self):
        reader = EvidenceReader((self.run,))
        path = self.run / "state.json"
        self.assertEqual(reader.json(path), self.state)
        path.chmod(path.stat().st_mode ^ 0o100)

        with self.assertRaisesRegex(EvidenceAccessError, "between reads"):
            reader.json(path)

    def test_dangling_continuations_link_is_invalid_not_an_empty_chain(self):
        continuations = self.run / "continuations"
        continuations.symlink_to(self.root / "missing", target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "real directory"):
            continuation_directories(continuations)

    def test_dangling_active_output_link_is_invalid(self):
        continuation = self.run / "continuations" / "01"
        continuation.mkdir(parents=True)
        continuation_input = {
            "schema_version": 1,
            "additional_responses": 1,
            "completed_responses": 0,
            "effective_max_responses": 1,
            "prior_output": "../../output.json",
        }
        active_state = {
            "schema_version": 1,
            "status": "running",
            "next_sequence": 1,
            "next_component": "attempt",
            "active_invocation": None,
            "history": [],
            "terminal": None,
            "continuation": continuation_input,
        }
        (continuation / "input.json").write_text(json.dumps(continuation_input))
        (continuation / "state.json").write_text(json.dumps(active_state))
        (continuation / "output.json").symlink_to(self.root / "missing-output")
        base_state = {
            "schema_version": 1,
            "status": "completed",
            "next_sequence": 1,
            "next_component": None,
            "active_invocation": None,
            "history": [],
            "terminal": {"decision": "exhausted"},
        }
        base_output = {
            "schema_version": 1,
            "outcome": "completed",
            "decision": "exhausted",
            "history": [],
        }

        with (
            mock.patch("afk_evidence.continuation.require_exhausted_structure"),
            self.assertRaisesRegex(ValueError, "not terminal"),
        ):
            observe_lineage(
                self.run,
                base_state,
                base_output,
                0,
                read_json=lambda path: json.loads(path.read_text()),
                locate_component=lambda *_args: self.run,
            )

    def test_deferred_exhaustion_proof_does_not_hide_bad_continuation_link(self):
        continuation = self.run / "continuations" / "01"
        continuation.mkdir(parents=True)
        continuation_input = {
            "schema_version": 1,
            "additional_responses": 1,
            "completed_responses": 0,
            "effective_max_responses": 1,
            "prior_output": "wrong-output.json",
        }
        continued_state = {
            "schema_version": 1,
            "status": "failed",
            "next_sequence": 2,
            "next_component": None,
            "active_invocation": None,
            "history": self.history,
            "terminal": self.state["terminal"],
            "continuation": continuation_input,
        }
        continued_output = {**self.output, "history": self.history}
        (continuation / "input.json").write_text(json.dumps(continuation_input))
        (continuation / "state.json").write_text(json.dumps(continued_state))
        (continuation / "output.json").write_text(json.dumps(continued_output))
        exhausted_state = {
            "schema_version": 1,
            "status": "completed",
            "next_sequence": 1,
            "next_component": None,
            "active_invocation": None,
            "history": [],
            "terminal": {"decision": "exhausted"},
        }
        exhausted_output = {
            "schema_version": 1,
            "outcome": "completed",
            "decision": "exhausted",
            "history": [],
        }

        unavailable = EvidenceUnavailable("missing evidence", "iteration/output.json")
        with (
            mock.patch(
                "afk_evidence.continuation.require_exhausted_structure",
                side_effect=unavailable,
            ),
            self.assertRaisesRegex(ValueError, "lineage"),
        ):
            observe_lineage(
                self.run,
                exhausted_state,
                exhausted_output,
                0,
                read_json=lambda path: json.loads(path.read_text()),
                locate_component=lambda *_args: self.run,
                defer_error=lambda error: isinstance(error, EvidenceUnavailable),
            )

    def test_unavailable_stage_proof_does_not_hide_later_stage_corruption(self):
        history = [
            {"sequence": 1, "component": "attempt", "outcome": "succeeded"},
            {"sequence": 2, "component": "validation", "outcome": "passed"},
            {"sequence": 3, "component": "change", "outcome": "completed"},
            {"sequence": 4, "component": "review", "outcome": "completed"},
        ]
        for row in history:
            row["directory"] = f"{row['sequence']:02d}-{row['component']}"
        assignment = {"workspace": str(self.root / "workspace")}
        source = SimpleNamespace(assignment=assignment, after={})
        lineage = SimpleNamespace(assignment=assignment)

        with (
            mock.patch(
                "afk_evidence.stages.verify_change_lineage", return_value=lineage
            ),
            mock.patch("afk_evidence.stages.verify_source", return_value=source),
            mock.patch(
                "afk_evidence.snapshot.load_passed_evidence",
                side_effect=EvidenceUnavailable(
                    "missing evidence", "validation/stdout.log"
                ),
            ),
            mock.patch(
                "afk_evidence.snapshot._verify_review",
                side_effect=RunValidationError("later Review is corrupt"),
            ),
            self.assertRaisesRegex(RunValidationError, "later Review is corrupt"),
        ):
            _verify_stage_provenance(
                mock.Mock(), history, (self.run,), TrustedContext(), assignment
            )

    def test_sealed_continuation_must_append_an_invocation(self):
        continuation_input = {
            "schema_version": 1,
            "additional_responses": 1,
            "completed_responses": 0,
            "effective_max_responses": 1,
            "prior_output": "../../output.json",
        }
        continued = {
            **self.state,
            "continuation": continuation_input,
        }
        with self.assertRaisesRegex(ValueError, "lineage"):
            validate_link(
                self.state, continued, continuation_input, "../../output.json"
            )

    def test_reader_pins_root_before_an_ancestor_is_replaced_by_a_symlink(self):
        reader = EvidenceReader((self.run,))
        retained = self.root / "retained-run"
        self.run.rename(retained)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "state.json").write_text('{"redirected":true}')
        self.run.symlink_to(outside, target_is_directory=True)

        self.assertEqual(reader.json(self.run / "state.json"), self.state)

    def test_failed_run_is_a_verified_observation_not_a_success(self):
        before = {
            path: path.read_bytes() for path in self.run.rglob("*") if path.is_file()
        }
        snapshot = read_run(
            self.run, "latest", TrustedContext(evidence_roots=(self.run,))
        )
        self.assertEqual(snapshot.proof.status, "verified")
        self.assertEqual(snapshot.selected_terminal.output["outcome"], "failed")
        self.assertEqual(snapshot.recorded_outcomes, ("failed",))
        self.assertIsNone(snapshot.candidate_commit)
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in self.run.rglob("*") if path.is_file()},
        )

    def test_missing_private_stage_proof_is_unavailable(self):
        (self.run / "01-attempt/output.json").unlink()
        snapshot = read_run(self.run, "latest", {"evidence_roots": [self.run]})
        self.assertEqual(snapshot.proof.status, "unavailable")
        self.assertIn("missing", snapshot.proof.reason)
        self.assertEqual(snapshot.latest_sealed_terminal.output["outcome"], "failed")

    def test_symlinked_proof_is_invalid_not_unavailable(self):
        target = self.root / "outside.json"
        target.write_text('{"schema_version":1,"outcome":"failed"}')
        (self.run / "01-attempt/output.json").unlink()
        (self.run / "01-attempt/output.json").symlink_to(target)
        with self.assertRaises(RunValidationError):
            read_run(self.run, "latest", {"evidence_roots": [self.run]})

    def test_oversized_private_json_is_unavailable_and_never_truncated(self):
        (self.run / "01-attempt/output.json").write_bytes(b" " * (1024 * 1024 + 1))
        snapshot = read_run(self.run, "latest", {"evidence_roots": [self.run]})
        self.assertEqual(snapshot.proof.status, "unavailable")
        self.assertIn("limit", snapshot.proof.reason)

    def test_oversized_preparation_is_not_treated_as_a_standalone_run(self):
        (self.run / "preparation.json").write_bytes(b" " * (1024 * 1024 + 1))

        snapshot = read_run(self.run, "latest", {"evidence_roots": [self.run]})

        self.assertEqual(snapshot.proof.status, "unavailable")
        self.assertIn("limit", snapshot.proof.reason)
        self.assertIsNone(snapshot.selected_terminal)

    def test_unavailable_early_output_does_not_hide_later_corruption(self):
        history = [
            {
                "sequence": 1,
                "component": "attempt",
                "directory": "01-attempt",
                "input_from": {"assignment": "assignment.json"},
                "outcome": "succeeded",
            },
            {
                "sequence": 2,
                "component": "validation",
                "directory": "02-validation",
                "input_from": {
                    "workspace": "assignment.json",
                    "change": "01-attempt",
                },
                "outcome": "failed",
            },
        ]
        terminal = {
            "failed_component": "validation",
            "component_outcome": "failed",
            "exit_code": 1,
        }
        state = {
            **self.state,
            "next_sequence": 3,
            "history": history,
            "terminal": terminal,
        }
        output = {
            **self.output,
            **terminal,
            "history": history,
        }
        self.write("state.json", state)
        self.write("output.json", output)
        (self.run / "01-attempt/output.json").unlink()
        (self.run / "02-validation").mkdir()
        self.write("02-validation/output.json", {"malformed": True})

        with self.assertRaisesRegex(RunValidationError, "invalid validation output"):
            read_run(self.run, "latest", {"evidence_roots": [self.run]})

    def test_related_work_uses_the_full_canonical_membership_contract(self):
        raw = b'{"id":"task","relationship":"subject","secret":"x"}\n'
        related = self.run / "related-work.jsonl"
        related.write_bytes(raw)
        reference = {
            "path": str(related),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "application/x-ndjson",
            "record_count": 1,
            "bytes": len(raw),
        }
        self.assignment["related_work"] = reference
        self.request["related_work"] = reference
        self.write("assignment.json", self.assignment)
        self.write("input.json", self.request)
        with self.assertRaises(RunValidationError):
            read_run(self.run, "latest", {"evidence_roots": [self.run]})


if __name__ == "__main__":
    unittest.main()
