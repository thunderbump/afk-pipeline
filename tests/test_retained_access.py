import json
import tempfile
import unittest
from pathlib import Path

from afk_records.access import (
    EvidenceAccessError,
    EvidenceReader,
    EvidenceUnavailable,
)


class RetainedEvidenceAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.state = {"schema_version": 1, "status": "failed"}
        (self.run / "state.json").write_text(json.dumps(self.state))

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

    def test_reader_pins_root_before_an_ancestor_is_replaced_by_a_symlink(self):
        reader = EvidenceReader((self.run,))
        retained = self.root / "retained-run"
        self.run.rename(retained)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "state.json").write_text('{"redirected":true}')
        self.run.symlink_to(outside, target_is_directory=True)

        self.assertEqual(reader.json(self.run / "state.json"), self.state)


if __name__ == "__main__":
    unittest.main()
