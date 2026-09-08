import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import afk_export
from afk_metrics.publication import (
    PublicationError,
    build_publication,
    load_publication_request,
    publish,
)
from tests.test_export_cli import ExportCliTests


class MetricsPublicationTests(unittest.TestCase):
    def fixture(self, root: Path, schema=3):
        source = ExportCliTests().sealed_preparer(root)
        bundle = root / "bundle"
        afk_export.export_run(source, bundle, schema_version=schema)
        request = {
            "schema_version": 1,
            "project": "operations-webui",
            "runs": [
                {
                    "source": str(source),
                    "bundle": str(bundle),
                    "selection": "latest",
                }
            ],
        }
        return source, bundle, request

    def test_valid_v2_and_v3_publications_are_bound_and_deterministic(self):
        for schema in (2, 3):
            with (
                self.subTest(schema=schema),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source, bundle, request = self.fixture(root, schema)
                input_path = root / "input.json"
                output = root / "publication.json"
                input_path.write_text(json.dumps(request))
                with mock.patch(
                    "afk_metrics.publication._source_revision", return_value="a" * 64
                ):
                    first = publish(input_path, output)
                    replay = build_publication(request)
                self.assertEqual(first, replay)
                self.assertEqual(
                    set(first),
                    {
                        "schema_version",
                        "kind",
                        "project",
                        "producer",
                        "runs",
                        "comparisons",
                        "limitations",
                    },
                )
                run = first["runs"][0]
                self.assertEqual(run["binding"]["bundle_schema_version"], schema)
                self.assertEqual(run["binding"]["run_id"], "run-example")
                self.assertEqual(
                    run["summary"]["source_identity"], run["summary"]["source_identity"]
                )
                self.assertTrue(
                    any(row["ownership"].get("sequence") == 2 for row in run["stages"])
                )
                serialized = output.read_text()
                self.assertNotIn(str(source), serialized)
                self.assertNotIn(str(bundle), serialized)

    def test_bundle_hash_and_semantic_mismatch_fail_without_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, bundle, request = self.fixture(root)
            workflow = bundle / "workflow-run.json"
            value = json.loads(workflow.read_text())
            value["response_limit"] += 1
            workflow.write_text(json.dumps(value))
            input_path = root / "input.json"
            destination = root / "publication.json"
            input_path.write_text(json.dumps(request))
            with self.assertRaises(PublicationError):
                publish(input_path, destination)
            self.assertFalse(destination.exists())

    def test_complete_bundle_manifest_inventory_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, bundle, request = self.fixture(root)
            (bundle / "unlisted.json").write_text("{}")
            with self.assertRaisesRegex(PublicationError, "inventory"):
                build_publication(request)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, bundle, request = self.fixture(root)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"].append(
                {"path": "missing.json", "bytes": 0, "sha256": "0" * 64}
            )
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(PublicationError):
                build_publication(request)

    def test_source_change_during_metrics_projection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            actual_summarize = __import__(
                "afk_metrics.publication", fromlist=["summarize_source"]
            ).summarize_source

            def summarize_then_change(*args, **kwargs):
                summary = actual_summarize(*args, **kwargs)
                (source / "coordinator" / "publication.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "succeeded",
                            "admission_outcome": "accepted",
                            "started_at": "2026-01-01T00:00:00Z",
                            "finished_at": "2026-01-01T00:00:01Z",
                            "process": {"exit_code": 0},
                            "error_category": None,
                        }
                    )
                )
                return summary

            with (
                mock.patch(
                    "afk_metrics.publication.summarize_source",
                    side_effect=summarize_then_change,
                ),
                self.assertRaisesRegex(PublicationError, "changed"),
            ):
                build_publication(request)

    def test_duplicate_identity_and_count_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            request["runs"].append(dict(request["runs"][0]))
            with self.assertRaisesRegex(PublicationError, "duplicate"):
                build_publication(request)
            request["runs"] = []
            path = root / "input.json"
            path.write_text(json.dumps(request))
            with self.assertRaises(PublicationError):
                load_publication_request(path)

    def test_destination_must_be_new_and_outside_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            with self.assertRaises(PublicationError):
                publish(input_path, source / "metrics.json")

    def test_output_limit_never_truncates_or_creates_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            destination = root / "publication.json"
            input_path.write_text(json.dumps(request))
            with (
                mock.patch("afk_metrics.publication.MAX_OUTPUT_BYTES", 1),
                self.assertRaisesRegex(PublicationError, "output exceeds"),
            ):
                publish(input_path, destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
