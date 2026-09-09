import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.metrics_publication_fixtures import FIXTURES, generate


class PopulatedPublicationTests(unittest.TestCase):
    def assert_cases(self, directory, publication):
        self.assertEqual(publication["schema_version"], 2)
        self.assertEqual(publication["producer"]["report_schema_version"], 2)
        self.assertEqual(len(publication["runs"]), 3)
        legacy = json.loads((directory / "producer-only-v2.json").read_text())
        self.assertEqual(legacy["schema_version"], 2)
        self.assertEqual(
            legacy["runs"][0]["summary"]["inference"]["evidence_coverage"],
            {
                "status": "partial",
                "expected": 3,
                "measured": 2,
                "missing": [
                    {
                        "ownership": {
                            "kind": "component",
                            "sequence": 1,
                            "component": "attempt",
                        },
                        "reason": "missing_receipt",
                    }
                ],
            },
        )
        self.assertEqual(legacy["runs"][0]["binding"]["bundle_schema_version"], 2)
        self.assertEqual(
            legacy["runs"][0]["binding"]["workflow_run_sha256"],
            hashlib.sha256(
                (directory / "producer-only-v2/workflow-run.json").read_bytes()
            ).hexdigest(),
        )
        for run, bundle_name in zip(
            publication["runs"],
            ("bundle-partial", "bundle-v3", "bundle-abandoned"),
            strict=True,
        ):
            bundle = directory / bundle_name
            self.assertEqual(
                run["binding"]["workflow_run_sha256"],
                hashlib.sha256((bundle / "workflow-run.json").read_bytes()).hexdigest(),
            )
            manifest = json.loads((bundle / "manifest.json").read_text())
            for item in manifest["files"]:
                raw = (bundle / item["path"]).read_bytes()
                self.assertEqual(len(raw), item["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            self.assertTrue(
                all(
                    row["ownership"]["run_id"] == run["binding"]["run_id"]
                    for row in run["stages"]
                )
            )
        for run in publication["runs"][:2]:
            self.assertEqual(
                run["summary"]["inference"]["evidence_coverage"],
                {"status": "complete", "expected": 3, "measured": 3, "missing": []},
            )
            invocations = {
                row["purpose"]: row
                for row in run["summary"]["inference"]["invocations"]
            }
            self.assertEqual(
                set(invocations),
                {"attempt", "review", "finding_assessment"},
            )
            review = invocations["review"]
            self.assertEqual(review["model"], "gpt-test")
            self.assertEqual(
                review["observed_identities"],
                [{"provider": "synthetic-provider", "model": "synthetic-observed"}],
            )
            metrics = review["metrics"]
            self.assertEqual(metrics["coverage"], "complete")
            self.assertEqual(metrics["usage"]["cacheRead"], 2)
            self.assertEqual(metrics["usage"]["cacheWrite"], 1)
            self.assertEqual(metrics["compaction"]["usage"]["input"], 10)
            self.assertEqual(metrics["cost"]["amount"], 0.03)
            self.assertEqual(
                metrics["cost"]["provenance"]["calculator"], "Pi model rates"
            )
            self.assertIsNone(metrics["cost"]["currency"])
            self.assertFalse(metrics["cost"]["billed_charge"])
            zero = invocations["finding_assessment"]["metrics"]
            self.assertEqual(zero["cost"]["amount"], 0)
            self.assertEqual(set(zero["usage"].values()), {0})
            attempt = invocations["attempt"]["metrics"]
            if run["binding"]["run_id"] == "populated-v3":
                self.assertEqual(attempt["coverage"], "unavailable")
                self.assertEqual(attempt["usage"], {})
                self.assertIsNone(attempt["cost"]["amount"])
                self.assertIn("provenance", attempt["cost"])
            else:
                self.assertEqual(attempt["coverage"], "partial")
                self.assertEqual(attempt["usage"], {"input": 4})
            rows = {
                row["ownership"].get("sequence"): row
                for row in run["stages"]
                if row["ownership"]["kind"] == "component"
            }
            self.assertEqual(rows[4]["usage"], metrics["usage"])
            self.assertEqual(rows[5]["usage"], zero["usage"])
            self.assertEqual(rows[1]["usage"], attempt["usage"])
            duration = None if run["binding"]["run_id"] == "populated-partial" else 0
            self.assertEqual(rows[2]["repository_validation_seconds"], duration)
        abandoned_summary = publication["runs"][2]["summary"]["inference"]
        self.assertEqual(abandoned_summary["evidence_coverage"]["expected"], 5)
        self.assertEqual(abandoned_summary["evidence_coverage"]["measured"], 0)
        self.assertEqual(abandoned_summary["totals"]["usage_coverage"], "unavailable")
        self.assertEqual(
            [
                item["reason"]
                for item in abandoned_summary["evidence_coverage"]["missing"][-2:]
            ],
            ["unsealed_receipt", "missing_receipt"],
        )
        abandoned = abandoned_summary["invocations"][0]
        self.assertEqual(
            abandoned["metrics"]["reason"], "unsealed_abandoned_invocation"
        )
        self.assertEqual(
            abandoned["metrics"]["cost"],
            {"status": "unavailable", "kind": "unavailable", "amount": None},
        )
        self.assertNotIn("observed_identities", abandoned)

    def test_committed_and_regenerated_cases_have_bound_measured_variants(self):
        self.assert_cases(
            FIXTURES, json.loads((FIXTURES / "valid-publication.json").read_text())
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "cases"
            publication = generate(destination)
            self.assert_cases(destination, publication)
            # Authenticated hashes include temporary private paths. Measurements
            # and stage projection, unlike those opaque identities, reproduce.
            committed = json.loads((FIXTURES / "valid-publication.json").read_text())
            for actual, expected in zip(
                publication["runs"], committed["runs"], strict=True
            ):
                self.assertEqual(actual["stages"], expected["stages"])
                self.assertEqual(
                    actual["summary"]["inference"]["totals"],
                    expected["summary"]["inference"]["totals"],
                )
                self.assertEqual(
                    actual["summary"]["inference"]["evidence_coverage"],
                    expected["summary"]["inference"]["evidence_coverage"],
                )
