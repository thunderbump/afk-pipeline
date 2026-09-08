import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from afk_evidence import RunValidationError, TrustedContext, read_run


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
