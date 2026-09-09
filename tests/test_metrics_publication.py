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
            fixtures = ExportCliTests()
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
                    ExportCliTests().add_inference_receipt(_inference)
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
            ExportCliTests().add_inference_receipt(target)
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

    def test_parent_swap_during_build_cannot_redirect_output_into_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            original_parent = root / "original-publication-parent"
            original_parent.mkdir()
            parent_alias = root / "publication-parent"
            parent_alias.symlink_to(original_parent, target_is_directory=True)
            destination = parent_alias / "publication.json"

            def build_then_swap(value):
                publication = build_publication(value)
                parent_alias.unlink()
                parent_alias.symlink_to(source, target_is_directory=True)
                return publication

            with (
                mock.patch(
                    "afk_metrics.publication.build_publication",
                    side_effect=build_then_swap,
                ),
                self.assertRaisesRegex(PublicationError, "cannot be created"),
            ):
                publish(input_path, destination)

            self.assertFalse((source / destination.name).exists())
            self.assertFalse((original_parent / destination.name).exists())

    def test_parent_swap_during_admission_is_detected_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            original_parent = root / "original-publication-parent"
            original_parent.mkdir()
            parent_alias = root / "publication-parent"
            parent_alias.symlink_to(original_parent, target_is_directory=True)
            destination = parent_alias / "publication.json"
            real_link = os.link

            def link_then_swap(*args, **kwargs):
                real_link(*args, **kwargs)
                parent_alias.unlink()
                parent_alias.symlink_to(source, target_is_directory=True)

            with (
                mock.patch("afk_metrics.publication.os.link", link_then_swap),
                self.assertRaisesRegex(PublicationError, "cannot be created"),
            ):
                publish(input_path, destination)

            self.assertFalse((source / destination.name).exists())
            self.assertFalse((original_parent / destination.name).exists())

    def test_publication_falls_back_when_anonymous_staging_is_unsupported(self):
        for unsupported in (errno.EOPNOTSUPP, errno.EINVAL):
            with (
                self.subTest(errno=unsupported),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                _source, _bundle, request = self.fixture(root)
                input_path = root / "input.json"
                input_path.write_text(json.dumps(request))
                destination = root / "publication.json"
                real_open = os.open
                temporary_flag = getattr(os, "O_TMPFILE", 0)

                def reject_anonymous_staging(
                    path,
                    flags,
                    *args,
                    _temporary_flag=temporary_flag,
                    _unsupported=unsupported,
                    _real_open=real_open,
                    **kwargs,
                ):
                    if _temporary_flag and flags & _temporary_flag == _temporary_flag:
                        raise OSError(_unsupported, "anonymous staging unsupported")
                    return _real_open(path, flags, *args, **kwargs)

                with mock.patch(
                    "afk_metrics.publication.os.open",
                    side_effect=reject_anonymous_staging,
                ):
                    publication = publish(input_path, destination)

                self.assertEqual(json.loads(destination.read_text()), publication)
                self.assertEqual(
                    [path for path in root.iterdir() if path.name.endswith(".tmp")],
                    [],
                )

    def test_publication_admits_the_written_anonymous_inode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _source, _bundle, request = self.fixture(root)
            input_path = root / "input.json"
            input_path.write_text(json.dumps(request))
            destination = root / "publication.json"
            real_link = os.link
            linked_descriptors = []

            def observe_link(source, *args, **kwargs):
                prefix = "/proc/self/fd/"
                self.assertTrue(source.startswith(prefix))
                descriptor = int(source.removeprefix(prefix))
                linked_descriptors.append(os.fstat(descriptor).st_ino)
                return real_link(source, *args, **kwargs)

            with mock.patch("afk_metrics.publication.os.link", observe_link):
                publication = publish(input_path, destination)

            self.assertEqual(len(linked_descriptors), 1)
            self.assertEqual(destination.stat().st_ino, linked_descriptors[0])
            self.assertEqual(json.loads(destination.read_text()), publication)


if __name__ == "__main__":
    unittest.main()
