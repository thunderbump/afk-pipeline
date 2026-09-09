import errno
import hashlib
import json
import os
import shutil
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
from tests import test_export_cli


class MetricsPublicationTests(unittest.TestCase):
    def fixture(self, root: Path, schema=3):
        source = test_export_cli.ExportCliTests().sealed_preparer(root)
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
                    run["summary"]["source_identity"],
                    hashlib.sha256(
                        json.dumps(
                            {
                                "identity": {
                                    "project": "operations-webui",
                                    "run_id": "run-example",
                                },
                                "assignment": json.loads(
                                    (source / "assignment.json").read_text()
                                ),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                )
                self.assertTrue(
                    any(row["ownership"].get("sequence") == 2 for row in run["stages"])
                )
                serialized = output.read_text()
                self.assertNotIn(str(source), serialized)
                self.assertNotIn(str(bundle), serialized)

    def test_stage_projection_preserves_measured_zero_and_unavailable_metrics(self):
        for duration in (None, 0, 1.5):
            with (
                self.subTest(duration=duration),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source = test_export_cli.ExportCliTests().sealed_preparer(root)
                validation_path = source / "coordinator/02-validation/output.json"
                validation = json.loads(validation_path.read_text())
                if duration is not None:
                    validation["duration_seconds"] = duration
                validation_path.write_text(json.dumps(validation))
                inference = source / "coordinator/04-review/inference"
                test_export_cli.ExportCliTests().add_inference_receipt(inference)
                # This authenticated Pi stream has only agent_end, without a
                # supported usage/cost event. Its elapsed time is still known.
                bundle = root / "bundle"
                afk_export.export_run(source, bundle)
                publication = build_publication(
                    {
                        "schema_version": 1,
                        "project": "operations-webui",
                        "runs": [
                            {
                                "source": str(source),
                                "bundle": str(bundle),
                                "selection": "original",
                            }
                        ],
                    }
                )
                rows = {
                    row["ownership"]["sequence"]: row
                    for row in publication["runs"][0]["stages"]
                    if row["ownership"]["kind"] == "component"
                }
                self.assertEqual(rows[2]["repository_validation_seconds"], duration)
                self.assertEqual(
                    rows[2]["repository_validation_coverage"],
                    "unavailable" if duration is None else "complete",
                )
                self.assertIsNone(rows[1]["elapsed_seconds"])
                self.assertEqual(rows[4]["elapsed_seconds"], 1)
                self.assertEqual(rows[4]["usage"], {})
                self.assertEqual(rows[4]["usage_coverage"], "unavailable")
                self.assertIsNone(rows[4]["cost"]["amount"])
                self.assertEqual(rows[4]["cost"]["status"], "unavailable")

    def test_run_and_aggregate_stage_limits_reject_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            path = root / "input.json"
            destination = root / "publication.json"
            path.write_text(json.dumps({**request, "runs": request["runs"] * 26}))
            with self.assertRaises(PublicationError):
                publish(path, destination)
            self.assertFalse(destination.exists())
            path.write_text(json.dumps(request))
            # Ten real fixture rows fit the reduced boundary exactly.
            with mock.patch("afk_metrics.publication.MAX_STAGES", 10):
                self.assertEqual(
                    len(build_publication(request)["runs"][0]["stages"]), 10
                )
            with (
                mock.patch("afk_metrics.publication.MAX_STAGES", 9),
                self.assertRaisesRegex(PublicationError, "stage count exceeds"),
            ):
                publish(path, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".afk-metrics-*")), [])

    def test_schema_version_requires_an_integer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, bundle, request = self.fixture(root)
            input_path = root / "input.json"
            for version in (True, 1.0):
                with self.subTest(version=version):
                    input_path.write_text(
                        json.dumps({**request, "schema_version": version})
                    )
                    with self.assertRaises(PublicationError):
                        load_publication_request(input_path)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["schema_version"] = 3.0
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(PublicationError):
                build_publication(request)

    def test_legacy_report_accepts_a_source_named_publish(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # An invalid source still produces an integrity report with exit 1;
            # argparse exit 2 would mean the legacy invocation was misrouted.
            (root / "publish").mkdir()
            for option in (
                ["--destination", str(root / "report")],
                ["--destination=" + str(root / "report-equals")],
            ):
                result = subprocess.run(
                    [sys.executable, "-m", "afk_metrics", "publish", *option],
                    cwd=root,
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(json.loads(result.stdout)["runs"], 1)

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
            unlisted = bundle / "unlisted"
            unlisted.mkdir()
            (unlisted / "unsafe").symlink_to(bundle / "manifest.json")
            # An undeclared tree is rejected at its root, not recursively
            # traversed (and therefore does not surface its unsafe child).
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

    def test_bundle_aggregate_limit_is_checked_before_payload_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, bundle, request = self.fixture(root)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][0]["bytes"] = afk_export.V2_MAX_BUNDLE_BYTES
            manifest_path.write_text(json.dumps(manifest))
            with (
                mock.patch(
                    "afk_metrics.publication.read_bytes_beneath",
                    side_effect=AssertionError("oversized payload was read"),
                ),
                self.assertRaisesRegex(PublicationError, "admission limits"),
            ):
                build_publication(request)

    def test_semantic_comparison_remains_artifact_free_while_evidence_is_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            calls = []
            actual_normalize = afk_export.normalize_run_v2

            def record_normalization(*args, **kwargs):
                calls.append(kwargs.get("include_artifacts", True))
                return actual_normalize(*args, **kwargs)

            with mock.patch(
                "afk_metrics.publication.normalize_run_v2",
                side_effect=record_normalization,
            ):
                publication = build_publication(request)
            self.assertEqual(len(publication["runs"]), 1)
            self.assertEqual(calls, [False, True, False, True])

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

    def test_metric_evidence_change_during_projection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            actual_summarize = __import__(
                "afk_metrics.publication", fromlist=["summarize_source"]
            ).summarize_source

            def summarize_then_change(*args, **kwargs):
                summary = actual_summarize(*args, **kwargs)
                events = source / "coordinator" / "01-attempt" / "events.jsonl"
                events.write_bytes(events.read_bytes() + b'{"type":"noop"}\n')
                return summary

            with (
                mock.patch(
                    "afk_metrics.publication.summarize_source",
                    side_effect=summarize_then_change,
                ),
                self.assertRaisesRegex(PublicationError, "changed"),
            ):
                build_publication(request)

    def test_transient_replacement_restored_before_confirmation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            output_path = source / "coordinator/02-validation/output.json"
            original = output_path.read_bytes()
            replacement = json.loads(original)
            replacement["duration_seconds"] = 99
            replacement_raw = (json.dumps(replacement) + "\n").encode()
            actual_summarize = __import__(
                "afk_metrics.publication", fromlist=["summarize_source"]
            ).summarize_source

            def summarize_replacement_then_restore(*args, **kwargs):
                output_path.write_bytes(replacement_raw)
                try:
                    return actual_summarize(*args, **kwargs)
                finally:
                    output_path.write_bytes(original)

            with (
                mock.patch(
                    "afk_metrics.publication.summarize_source",
                    side_effect=summarize_replacement_then_restore,
                ),
                self.assertRaisesRegex(PublicationError, "metrics verification"),
            ):
                build_publication(request)

    def test_abandoned_inference_change_during_projection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = test_export_cli.ExportCliTests()
            source = fixtures.sealed_preparer(root)
            history = [
                fixtures.history()[0],
                {**fixtures.history()[1], "outcome": "failed"},
                {
                    "sequence": 3,
                    "component": "response",
                    "directory": "03-response",
                    "input_from": {"validation": "02-validation"},
                    "outcome": "abandoned",
                },
            ]
            state_path = source / "coordinator" / "state.json"
            state = json.loads(state_path.read_text())
            state.update(
                status="failed",
                next_sequence=4,
                history=history,
                terminal={
                    "failed_component": "validation",
                    "component_outcome": "failed",
                    "exit_code": 1,
                },
            )
            state_path.write_text(json.dumps(state))
            validation_output_path = (
                source / "coordinator" / "02-validation" / "output.json"
            )
            validation_output = json.loads(validation_output_path.read_text())
            validation_output["outcome"] = "failed"
            validation_output_path.write_text(json.dumps(validation_output))
            output_path = source / "coordinator" / "output.json"
            output_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "failed",
                        **state["terminal"],
                        "history": history,
                    }
                )
            )
            preparation_path = source / "preparation.json"
            preparation = json.loads(preparation_path.read_text())
            preparation["coordinator"].update(
                status="failed", exit_code=1, outcome="failed", decision=None
            )
            preparation_path.write_text(json.dumps(preparation))
            stage = source / "coordinator" / "03-response"
            (source / "coordinator" / "03-change").rename(stage)
            inference = stage / "inference"
            fixtures.add_inference_receipt(inference)
            invocation_path = inference / "invocation.json"
            invocation = json.loads(invocation_path.read_text())
            invocation["purpose"] = "feedback_response"
            invocation_path.write_text(json.dumps(invocation) + "\n")
            receipt_path = inference / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["hashes"]["invocation_sha256"] = hashlib.sha256(
                invocation_path.read_bytes()
            ).hexdigest()
            receipt_path.write_text(json.dumps(receipt) + "\n")
            bundle = root / "bundle"
            afk_export.export_run(source, bundle, schema_version=3)
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
            actual_summarize = __import__(
                "afk_metrics.publication", fromlist=["summarize_source"]
            ).summarize_source

            def summarize_then_change(*args, **kwargs):
                summary = actual_summarize(*args, **kwargs)
                events = inference / "attempts" / "1" / "events.jsonl"
                events.write_bytes(events.read_bytes() + b'{"type":"noop"}\n')
                return summary

            with (
                mock.patch(
                    "afk_metrics.publication.summarize_source",
                    side_effect=summarize_then_change,
                ),
                self.assertRaisesRegex(PublicationError, "changed"),
            ):
                build_publication(request)

            # An abandoned invocation with no receipt is represented by its
            # checkpointed availability state. A directory created only while
            # metrics are calculated must not become a new interrupted stage.
            shutil.rmtree(inference)

            def summarize_with_transient_directory(*args, **kwargs):
                inference.mkdir(parents=True)
                try:
                    return actual_summarize(*args, **kwargs)
                finally:
                    shutil.rmtree(inference)

            with mock.patch(
                "afk_metrics.publication.summarize_source",
                side_effect=summarize_with_transient_directory,
            ):
                publication = build_publication(request)
            self.assertFalse(
                any(
                    invocation["purpose"] == "feedback_response"
                    for invocation in publication["runs"][0]["summary"]["inference"][
                        "invocations"
                    ]
                )
            )

            # Likewise, a directory captured as unsealed must not be upgraded
            # when a complete receipt appears only during metrics projection.
            inference.mkdir(parents=True)

            def summarize_with_transient_receipt(*args, **kwargs):
                inference.rmdir()
                fixtures.add_inference_receipt(inference)
                invocation_path = inference / "invocation.json"
                invocation = json.loads(invocation_path.read_text())
                invocation["purpose"] = "feedback_response"
                invocation_path.write_text(json.dumps(invocation) + "\n")
                receipt_path = inference / "receipt.json"
                receipt = json.loads(receipt_path.read_text())
                receipt["hashes"]["invocation_sha256"] = hashlib.sha256(
                    invocation_path.read_bytes()
                ).hexdigest()
                receipt_path.write_text(json.dumps(receipt) + "\n")
                try:
                    return actual_summarize(*args, **kwargs)
                finally:
                    shutil.rmtree(inference)
                    inference.mkdir()

            with mock.patch(
                "afk_metrics.publication.summarize_source",
                side_effect=summarize_with_transient_receipt,
            ):
                publication = build_publication(request)
            response = next(
                row
                for row in publication["runs"][0]["summary"]["inference"]["invocations"]
                if row["purpose"] == "feedback_response"
            )
            self.assertEqual(
                response["metrics"]["reason"], "unsealed_abandoned_invocation"
            )

    def test_transient_planner_and_component_inference_are_not_published(self):
        for relative, purpose in (
            ("planner/inference", "acceptance_planning"),
            ("coordinator/01-attempt/inference", "attempt"),
        ):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source, _bundle, request = self.fixture(root)
                inference = source / relative
                actual_summarize = __import__(
                    "afk_metrics.publication", fromlist=["summarize_source"]
                ).summarize_source

                def summarize_with_transient_inference(
                    *args,
                    _inference=inference,
                    _purpose=purpose,
                    _relative=relative,
                    _summarize=actual_summarize,
                    **kwargs,
                ):
                    _inference.parent.mkdir(parents=True, exist_ok=True)
                    test_export_cli.ExportCliTests().add_inference_receipt(_inference)
                    invocation_path = _inference / "invocation.json"
                    invocation = json.loads(invocation_path.read_text())
                    invocation["purpose"] = _purpose
                    invocation_path.write_text(json.dumps(invocation) + "\n")
                    receipt_path = _inference / "receipt.json"
                    receipt = json.loads(receipt_path.read_text())
                    receipt["hashes"]["invocation_sha256"] = hashlib.sha256(
                        invocation_path.read_bytes()
                    ).hexdigest()
                    receipt_path.write_text(json.dumps(receipt) + "\n")
                    try:
                        return _summarize(*args, **kwargs)
                    finally:
                        shutil.rmtree(_inference)
                        if _relative == "planner/inference":
                            _inference.parent.rmdir()

                with mock.patch(
                    "afk_metrics.publication.summarize_source",
                    side_effect=summarize_with_transient_inference,
                ):
                    publication = build_publication(request)
                self.assertFalse(
                    any(
                        row["purpose"] == purpose
                        for row in publication["runs"][0]["summary"]["inference"][
                            "invocations"
                        ]
                    )
                )

    def test_transient_in_tree_symlink_cannot_bypass_inference_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            target = source / "coordinator/04-review/transient-inference"
            test_export_cli.ExportCliTests().add_inference_receipt(target)
            invocation_path = target / "invocation.json"
            invocation = json.loads(invocation_path.read_text())
            invocation["purpose"] = "finding_assessment"
            invocation_path.write_text(json.dumps(invocation) + "\n")
            receipt_path = target / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["hashes"]["invocation_sha256"] = hashlib.sha256(
                invocation_path.read_bytes()
            ).hexdigest()
            receipt_path.write_text(json.dumps(receipt) + "\n")
            inference = source / "coordinator/05-assessment/inference"
            actual_summarize = __import__(
                "afk_metrics.publication", fromlist=["summarize_source"]
            ).summarize_source

            def summarize_with_transient_symlink(*args, **kwargs):
                inference.symlink_to("../04-review/transient-inference")
                try:
                    return actual_summarize(*args, **kwargs)
                finally:
                    inference.unlink()

            with mock.patch(
                "afk_metrics.publication.summarize_source",
                side_effect=summarize_with_transient_symlink,
            ):
                publication = build_publication(request)
            self.assertFalse(
                any(
                    row["purpose"] == "finding_assessment"
                    for row in publication["runs"][0]["summary"]["inference"][
                        "invocations"
                    ]
                )
            )

    def test_publication_input_read_is_bounded_and_requires_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "oversized.json"
            with oversized.open("wb") as stream:
                stream.truncate(1024 * 1024 + 1)
            with self.assertRaisesRegex(PublicationError, "size limit"):
                load_publication_request(oversized)

            directory = root / "directory"
            directory.mkdir()
            with self.assertRaisesRegex(PublicationError, "regular file"):
                load_publication_request(directory)
            zero = Path("/dev/zero")
            if zero.exists():
                with self.assertRaisesRegex(PublicationError, "regular file"):
                    load_publication_request(zero)

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

    def test_named_staging_success_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, bundle, request = self.fixture(root)
            before = {
                path: path.read_bytes()
                for directory in (source, bundle)
                for path in directory.rglob("*")
                if path.is_file()
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            parent = root / "output"
            parent.mkdir()
            alias = root / "alias"
            alias.symlink_to(parent, target_is_directory=True)
            destination = alias / ("p" * 255)
            result = publish(input_path, destination)
            self.assertEqual(json.loads(destination.read_text()), result)
            self.assertEqual(list(parent.iterdir()), [parent / destination.name])
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_existing_files_and_dangling_symlinks_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            existing = root / "existing.json"
            existing.write_text("previous owner")
            dangling = root / "dangling.json"
            dangling.symlink_to(root / "absent")
            for destination in (existing, dangling):
                with (
                    self.subTest(path=destination.name),
                    self.assertRaises(PublicationError),
                ):
                    publish(input_path, destination)
            self.assertEqual(existing.read_text(), "previous owner")
            self.assertTrue(dangling.is_symlink())
            self.assertEqual(list(root.glob(".afk-metrics-*.tmp")), [])

    def test_concurrent_publishers_admit_one_complete_file(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            destination = root / "publication.json"
            barrier = Barrier(2)
            real_link = os.link

            def admit(staging, target):
                self.assertFalse(destination.exists())
                self.assertEqual(
                    json.loads(Path(staging).read_text())["kind"],
                    "afk-metrics-publication",
                )
                barrier.wait(timeout=10)
                return real_link(staging, target)

            def run(_index):
                try:
                    return publish(input_path, destination)
                except PublicationError:
                    return None

            with (
                mock.patch("afk_metrics.publication.os.link", admit),
                ThreadPoolExecutor(2) as pool,
            ):
                results = list(pool.map(run, range(2)))
            winners = [value for value in results if value is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(json.loads(destination.read_text()), winners[0])
            self.assertEqual(list(root.glob(".afk-metrics-*.tmp")), [])

    def test_temporary_name_collisions_never_delete_unowned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            collider = root / ".afk-metrics-collision.tmp"
            collider.write_text("unowned")
            real_open = os.open
            collisions = 0

            def open_with_collision(path, flags, *args, **kwargs):
                nonlocal collisions
                if flags & os.O_EXCL and Path(path).name.startswith(".afk-metrics-"):
                    collisions += 1
                    # Exercise the OS exclusive-create failure, rather than
                    # inventing ownership of the name that failed allocation.
                    return real_open(collider, flags, *args, **kwargs)
                return real_open(path, flags, *args, **kwargs)

            with (
                mock.patch("afk_metrics.publication.os.open", open_with_collision),
                mock.patch("tempfile.TMP_MAX", 3),
                self.assertRaises(PublicationError),
            ):
                publish(input_path, root / "publication.json")
            self.assertEqual(collisions, 3)
            self.assertEqual(collider.read_text(), "unowned")
            self.assertEqual(list(root.glob(".afk-metrics-*.tmp")), [collider])

    def test_precommit_io_failures_clean_staging_without_advertising_output(self):
        for operation in ("fsync", "link"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                _source, _bundle, request = self.fixture(root)
                input_path = root / "input.json"
                input_path.write_text(json.dumps(request))
                destination = root / "publication.json"
                # Unsupported hard links must not fall back to direct writes.
                error = OSError(
                    errno.EOPNOTSUPP if operation == "link" else errno.EIO,
                    "injected IO failure",
                )
                with (
                    mock.patch(
                        f"afk_metrics.publication.os.{operation}", side_effect=error
                    ),
                    self.assertRaises(PublicationError),
                ):
                    publish(input_path, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob(".afk-metrics-*.tmp")), [])

    def test_failed_write_keeps_final_name_absent_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            destination = root / "publication.json"
            real_fdopen = os.fdopen

            def fdopen(descriptor, mode, **kwargs):
                stream = real_fdopen(descriptor, mode, **kwargs)
                if mode != "wb":
                    return stream
                proxy = mock.MagicMock(wraps=stream)
                proxy.__enter__.return_value = proxy
                proxy.__exit__.side_effect = lambda *_args: stream.close()

                def fail_write(raw):
                    self.assertFalse(destination.exists())
                    stream.write(raw[:10])
                    raise OSError(errno.ENOSPC, "disk full")

                proxy.write.side_effect = fail_write
                return proxy

            with (
                mock.patch("afk_metrics.publication.os.fdopen", fdopen),
                self.assertRaises(PublicationError),
            ):
                publish(input_path, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".afk-metrics-*.tmp")), [])

    def test_cleanup_failure_reports_whether_publication_committed(self):
        for committed in (False, True):
            with (
                self.subTest(committed=committed),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                _source, _bundle, request = self.fixture(root)
                input_path = root / "input.json"
                input_path.write_text(json.dumps(request))
                destination = root / "publication.json"
                real_link = os.link

                def link(*args, committed=committed, real_link=real_link):
                    if not committed:
                        raise OSError(errno.EIO, "cannot link")
                    return real_link(*args)

                expected = (
                    "publication committed; staging cleanup failed"
                    if committed
                    else "publication failed before commit; staging cleanup failed"
                )
                with (
                    mock.patch("afk_metrics.publication.os.link", link),
                    mock.patch(
                        "afk_metrics.publication.os.unlink",
                        side_effect=OSError(errno.EIO, "cannot clean"),
                    ),
                    self.assertRaisesRegex(PublicationError, expected),
                ):
                    publish(input_path, destination)
                self.assertEqual(destination.exists(), committed)
                if committed:
                    self.assertEqual(
                        json.loads(destination.read_text())["kind"],
                        "afk-metrics-publication",
                    )
                self.assertEqual(len(list(root.glob(".afk-metrics-*.tmp"))), 1)


if __name__ == "__main__":
    unittest.main()
