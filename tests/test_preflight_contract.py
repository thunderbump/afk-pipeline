import unittest

from afk_preflight.contract import validate_output


class PreflightContractTest(unittest.TestCase):
    def test_noncompleted_output_cannot_claim_unvalidated_requests(self):
        preflight_input = {"source": {"kind": "bead", "id": "central-123"}}
        output = {
            "schema_version": 1,
            "outcome": "failed",
            "source": preflight_input["source"],
            "decision": "pause",
            "started_at": "2026-08-18T00:00:00Z",
            "finished_at": "2026-08-18T00:00:01Z",
            "duration_seconds": 1,
            "process": {"exit_code": 1, "signal": None},
            "agent": None,
            "classifier": {
                "kind": "inference",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "status": "failed",
            },
            "requests": [{"untrusted": "partial classifier output"}],
            "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
        }

        with self.assertRaisesRegex(ValueError, "without a request ledger"):
            validate_output(output, preflight_input)


if __name__ == "__main__":
    unittest.main()
