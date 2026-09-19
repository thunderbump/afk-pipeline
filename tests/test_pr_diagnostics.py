import json
import tempfile
import unittest
from pathlib import Path

from afk_pr.diagnostics import public_summary


class DiagnosticTests(unittest.TestCase):
    def test_invalid_or_missing_summary_is_optional_and_never_published(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            evidence = directory / "fixture-evidence"
            evidence.mkdir()
            path = evidence / "public-summary.json"
            job = {"head": "a" * 40, "repository": "/private/repo"}
            valid = {
                "schema_version": 1,
                "head": job["head"],
                "profile": "profile",
                "status": "failed",
                "step": "fixture_preparation",
                "diagnostic_codes": ["baseline_unavailable"],
                "timings_ms": {"validation": 10, "restore": None},
            }
            self.assertEqual(public_summary(directory, job), "")
            path.write_text(json.dumps(valid))
            self.assertIn("baseline_unavailable", public_summary(directory, job))
            for raw in (
                "broken",
                "[" * 3000 + "]" * 3000,
                "x" * 8193,
                "[]",
                json.dumps({**valid, "head": "b" * 40}),
                json.dumps({**valid, "private_path": "/secret"}),
                json.dumps({**valid, "diagnostic_codes": ["/secret/path"]}),
                json.dumps(
                    {**valid, "timings_ms": {"validation": True, "restore": None}}
                ),
                json.dumps({**valid, "schema_version": True}),
            ):
                path.write_text(raw)
                self.assertEqual(public_summary(directory, job), "")
            path.unlink()
            target = directory / "private.json"
            target.write_text(json.dumps(valid))
            path.symlink_to(target)
            self.assertEqual(public_summary(directory, job), "")


if __name__ == "__main__":
    unittest.main()
