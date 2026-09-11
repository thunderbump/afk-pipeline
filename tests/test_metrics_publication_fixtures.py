import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.metrics_publication_fixtures import (
    FIXTURES,
    generate,
    generate_baselines,
    review_variant_matrix,
)


class PopulatedPublicationTests(unittest.TestCase):
    def assert_current_summary(self, publication):
        for run in publication["runs"]:
            self.assertIn("review_stages", run["summary"])
            for invocation in run["summary"]["inference"]["invocations"]:
                self.assertIn("ownership", invocation)

    def test_baselines_use_current_summary_contract(self):
        self.assert_current_summary(
            json.loads((FIXTURES.parent / "valid-publication.json").read_text())
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "baseline"
            generate_baselines(destination)
            publication = json.loads(
                (destination / "valid-publication.json").read_text()
            )
            self.assert_current_summary(publication)
            for run in publication["runs"]:
                bundle = (
                    destination / f"bundle-v{run['binding']['bundle_schema_version']}"
                )
                self.assertEqual(
                    hashlib.sha256(
                        (bundle / "workflow-run.json").read_bytes()
                    ).hexdigest(),
                    run["binding"]["workflow_run_sha256"],
                )
            invalid = json.loads((destination / "invalid-publication.json").read_text())
            self.assertEqual(
                invalid["runs"][0]["binding"]["workflow_run_sha256"], "0" * 64
            )

    def assert_cases(self, directory, publication):
        self.assert_current_summary(publication)
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
            self.assertEqual(metrics["cost"]["currency"], "USD")
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

    def assert_split_cases(self, directory):
        publication = json.loads((directory / "split-publication.json").read_text())
        self.assertEqual(publication["schema_version"], 2)
        for name, run, measured in zip(
            ("split-completed", "split-partial", "split-continuation"),
            publication["runs"],
            (3, 2, 6),
            strict=True,
        ):
            bundle = directory / f"bundle-{name}"
            raw = (bundle / "workflow-run.json").read_bytes()
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(), run["binding"]["workflow_run_sha256"]
            )
            self.assertNotIn(b"/tmp/", raw)
            workflow = json.loads(raw)
            invocations = run["summary"]["inference"]["invocations"]
            self.assertEqual(len(invocations), measured)
            owners = [
                json.dumps(row["ownership"], sort_keys=True) for row in invocations
            ]
            self.assertEqual(len(owners), len(set(owners)))
            for stage in run["summary"]["review_stages"]:
                self.assertEqual(stage["mode"], "split")
                self.assertEqual(
                    stage["elapsed"], {"seconds": 4, "coverage": "complete"}
                )
            review = workflow["history"][3]["output"]["details"]
            if name == "split-partial":
                self.assertEqual(len(workflow["history"]), 4)
                self.assertEqual(
                    [row["outcome"] for row in review["reviewer_invocations"]],
                    ["succeeded", "timed_out", "unstarted"],
                )
                missing = run["summary"]["inference"]["evidence_coverage"]["missing"]
                self.assertIn(
                    "standards", [row["ownership"].get("lens") for row in missing]
                )
            else:
                self.assertEqual(
                    [
                        len(row["review"]["findings"])
                        for row in review["reviewer_invocations"]
                    ],
                    [2, 0, 0],
                )
                self.assertEqual(review["findings"][0], review["findings"][1])
                self.assertEqual(
                    [
                        row["source_finding_index"]
                        for row in review["finding_provenance"]
                    ],
                    [0, 1],
                )
                decisions = workflow["history"][4]["output"]["details"]["decisions"]
                self.assertEqual([row["finding_index"] for row in decisions], [0, 1])
            if name == "split-continuation":
                self.assertTrue(run["binding"]["run_id"].endswith(".continuation.01"))
                self.assertEqual(
                    [row["ownership"]["sequence"] for row in invocations].count(4), 3
                )

    def test_generated_review_variant_matrix_preserves_failure_and_provenance(self):
        committed = json.loads((FIXTURES / "review-variants.json").read_text())
        self.assertEqual(committed, review_variant_matrix())
        cases = {case["name"]: case for case in committed["cases"]}
        self.assertEqual(cases["combined-default"]["effective_mode"], "combined")
        split = cases["split-empty-and-duplicates"]
        self.assertEqual(
            [len(row["findings"]) for row in split["invocations"]], [2, 0, 0]
        )
        self.assertEqual(len(split["aggregate"]["findings"]), 2)
        self.assertEqual(
            [row["source_finding_index"] for row in split["aggregate"]["provenance"]],
            [0, 1],
        )
        partial = cases["split-partial-failure"]
        self.assertIsNone(partial["aggregate"])
        self.assertFalse(partial["assessment_started"])
        continuation = cases["split-abandoned-continuation"]["continuation"]
        self.assertEqual(
            {continuation[key] for key in ("repair", "resume", "exhausted")},
            {"split"},
        )
        self.assertFalse(continuation["reuse_partial_invocations"])

    def test_committed_and_regenerated_cases_have_bound_measured_variants(self):
        self.assert_split_cases(FIXTURES)
        self.assert_cases(
            FIXTURES, json.loads((FIXTURES / "valid-publication.json").read_text())
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "cases"
            publication = generate(destination)
            self.assert_split_cases(destination)
            self.assert_cases(destination, publication)
            self.assertEqual(
                json.loads((destination / "review-variants.json").read_text()),
                review_variant_matrix(),
            )
            committed_coverage = json.loads(
                (FIXTURES / "evidence-coverage-variants.json").read_text()
            )
            coverage_publication = json.loads(
                (destination / "evidence-coverage-variants.json").read_text()
            )
            self.assert_current_summary(committed_coverage)
            self.assert_current_summary(coverage_publication)
            self.assertEqual(coverage_publication["schema_version"], 2)
            self.assertEqual(
                [
                    run["summary"]["inference"]["evidence_coverage"]
                    for run in coverage_publication["runs"]
                ],
                [
                    {"status": "complete", "expected": 5, "measured": 5, "missing": []},
                    {"status": "complete", "expected": 6, "measured": 6, "missing": []},
                ],
            )
            no_action, shared = coverage_publication["runs"]
            self.assertNotIn(
                "feedback_response",
                {
                    row["purpose"]
                    for row in no_action["summary"]["inference"]["invocations"]
                },
            )
            self.assertEqual(
                shared["binding"]["run_id"].split(".")[-2:],
                ["continuation", "01"],
            )
            self.assertEqual(
                [row["ownership"].get("sequence") for row in shared["stages"]].count(4),
                1,
            )
            for actual, expected in zip(
                coverage_publication["runs"], committed_coverage["runs"], strict=True
            ):
                self.assertEqual(actual["stages"], expected["stages"])
                self.assertEqual(
                    actual["summary"]["inference"]["evidence_coverage"],
                    expected["summary"]["inference"]["evidence_coverage"],
                )
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
