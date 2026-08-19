import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import afk_export

ROOT = Path(__file__).parents[1]


class ExportCliTests(unittest.TestCase):
    def test_exports_a_sealed_preparer_run_as_a_portable_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            destination = root / "bundle"

            result = self.export(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["outcome"], "exported")
            self.assertEqual(
                output["identity"],
                {"project": "operations-webui", "run_id": "run-example"},
            )
            manifest = json.loads((destination / "manifest.json").read_text())
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(manifest["identity"], output["identity"])
            self.assertEqual(record["bead"], {"id": "central-example"})
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["terminal"], {"decision": "stop"})
            self.assertEqual(
                [item["sequence"] for item in record["history"]], list(range(1, 7))
            )
            self.assertEqual(
                record["history"][3]["output"]["details"]["kind"], "review"
            )
            included = [
                item for item in record["evidence"] if item["inclusion"] == "included"
            ]
            self.assertEqual({item["kind"] for item in included}, {"stdout", "diff"})
            events = [item for item in record["evidence"] if item["kind"] == "events"]
            self.assertTrue(events)
            self.assertTrue(all(item["inclusion"] == "omitted" for item in events))
            self.assertEqual(events[0]["event_counts"]["agent_end"], 1)
            self.assertFalse(
                any(path.name == "events.jsonl" for path in destination.rglob("*"))
            )

            inventory = {item["path"]: item for item in manifest["files"]}
            self.assertEqual(
                set(inventory),
                {
                    "workflow-run.json",
                    "evidence/02-validation/stdout.txt",
                    "evidence/04-review/diff.patch",
                },
            )
            for relative, item in inventory.items():
                payload = (destination / relative).read_bytes()
                self.assertEqual(item["bytes"], len(payload))
                self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())
            rendered = b"".join(
                path.read_bytes() for path in destination.rglob("*") if path.is_file()
            )
            self.assertNotIn(str(root).encode(), rendered)
            self.assertNotIn(b"should-not-export", rendered)

            second = root / "second-bundle"
            self.assertEqual(self.export(source, second).returncode, 0)
            first_files = {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(second): path.read_bytes()
                for path in second.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first_files, second_files)

    def test_exports_a_direct_coordinator_with_explicit_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root) / "coordinator"
            destination = root / "direct-bundle"

            missing = self.export(source, destination)
            self.assertEqual(missing.returncode, 2)
            self.assertFalse(destination.exists())

            result = self.export(
                source,
                destination,
                "--project",
                "operations-webui",
                "--run-id",
                "direct-1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(record["identity"]["run_id"], "direct-1")
            self.assertEqual(record["bead"], {"id": "central-example"})

            credential = root / "credential-bundle"
            rejected = self.export(
                source,
                credential,
                "--project",
                "operations-webui",
                "--run-id",
                f"ghp_{'a' * 30}",
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertFalse(credential.exists())

    def test_unsealed_or_malformed_evidence_leaves_no_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            destination = root / "bundle"
            (source / "coordinator" / "output.json").unlink()

            result = self.export(source, destination)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(destination.exists())
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "schema_version": 1,
                    "outcome": "rejected",
                    "error": "invalid_run",
                },
            )

    def test_oversized_allowlisted_evidence_leaves_no_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            stdout = source / "coordinator" / "02-validation" / "stdout.log"
            with stdout.open("wb") as stream:
                stream.truncate(2 * 1024 * 1024)
            destination = root / "bundle"

            result = self.export(source, destination)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(destination.exists())

    def test_destination_symlink_cannot_redirect_the_bundle_into_the_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            alias = root / "source-alias"
            alias.symlink_to(source, target_is_directory=True)

            result = self.export(source, alias / "bundle")

            self.assertEqual(result.returncode, 1)
            self.assertFalse((source / "bundle").exists())

    def test_allowlisted_evidence_redacts_paths_and_blocks_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            stdout = source / "coordinator" / "02-validation" / "stdout.log"
            paths = (
                f"workspace={root / 'workspace'}\n",
                "config=/run/secrets/service-token\n",
                "home=/Users/brian/private\n",
            )
            for index, value in enumerate(paths):
                with self.subTest(value=value):
                    stdout.write_text(value)
                    destination = root / f"bundle-{index}"
                    result = self.export(source, destination)
                    self.assertEqual(result.returncode, 0)
                    exported = (
                        destination / "evidence/02-validation/stdout.txt"
                    ).read_text()
                    self.assertIn("[redacted-path]", exported)
                    self.assertNotIn(value.rstrip(), exported)

            credentials = (
                f"token=ghp_{'a' * 30}\n",
                "Authorization: Bearer eyJheader.eyJpayload.signature\n",
                "AWS_SECRET_ACCESS_KEY=not-a-public-value\n",
            )
            for index, value in enumerate(credentials):
                with self.subTest(value=value):
                    stdout.write_text(value)
                    destination = root / f"blocked-bundle-{index}"
                    result = self.export(source, destination)
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(destination.exists())

    def test_sensitive_normalized_text_is_redacted_or_blocks_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            for path in (
                source / "assignment.json",
                source / "coordinator" / "assignment.json",
            ):
                assignment = json.loads(path.read_text())
                assignment["objective"] = "Read /Users/brian/private before work."
                path.write_text(json.dumps(assignment))
            destination = root / "bundle"

            result = self.export(source, destination)

            self.assertEqual(result.returncode, 0)
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(record["objective"], "Read [redacted-path] before work.")

            for path in (
                source / "assignment.json",
                source / "coordinator" / "assignment.json",
            ):
                assignment = json.loads(path.read_text())
                assignment["objective"] = (
                    "Authorization: Bearer eyJheader.eyJpayload.signature"
                )
                path.write_text(json.dumps(assignment))
            blocked = root / "blocked-bundle"

            result = self.export(source, blocked)

            self.assertEqual(result.returncode, 1)
            self.assertFalse(blocked.exists())

    def test_exports_a_sealed_failed_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            coordinator = source / "coordinator"
            history = self.history()[:4]
            history[-1] = {**history[-1], "outcome": "failed"}
            terminal = {
                "failed_component": "review",
                "component_outcome": "failed",
                "exit_code": 1,
            }
            state = {
                "schema_version": 1,
                "status": "failed",
                "next_sequence": 5,
                "next_component": None,
                "active_invocation": None,
                "history": history,
                "terminal": terminal,
            }
            (coordinator / "state.json").write_text(json.dumps(state))
            (coordinator / "output.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "failed",
                        **terminal,
                        "history": history,
                    }
                )
            )
            review_path = coordinator / "04-review" / "output.json"
            review_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "failed",
                        "process": {"exit_code": 1, "signal": None},
                        "agent": {"status": "error"},
                        "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
                    }
                )
            )
            preparation_path = source / "preparation.json"
            preparation = json.loads(preparation_path.read_text())
            preparation["coordinator"].update(
                status="failed", exit_code=1, outcome="failed", decision=None
            )
            preparation_path.write_text(json.dumps(preparation))
            destination = root / "bundle"

            result = self.export(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["terminal"], terminal)
            self.assertEqual(
                record["history"][-1]["output"]["details"], {"kind": "review"}
            )

    def test_v2_publishes_semantic_artifacts_and_large_events_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            events = source / "coordinator/01-attempt/events.jsonl"
            line = b'{"type":"message_update","path":"/Users/private/work"}\n'
            events.write_bytes(line * ((9 * 1024 * 1024 // len(line)) + 1))
            private = events.read_bytes()
            destination = root / "v2-bundle"

            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(record["schema_version"], 2)
            artifacts = {item["source"]["path"]: item for item in record["artifacts"]}
            event = artifacts["coordinator/01-attempt/events.jsonl"]
            self.assertEqual(event["state"], "published")
            self.assertGreater(event["public_bytes"], 8 * 1024 * 1024)
            self.assertEqual(event["sanitization_status"], "sanitized")
            self.assertEqual(events.read_bytes(), private)
            public = (destination / event["path"]).read_bytes()
            self.assertNotIn(b"/Users/private/work", public)
            self.assertEqual(hashlib.sha256(public).hexdigest(), event["public_sha256"])

    def test_v2_redacts_a_command_credential_value_from_assignment_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            secret = "opaque-command-credential"
            for path in (
                source / "assignment.json",
                source / "coordinator/assignment.json",
            ):
                assignment = json.loads(path.read_text())
                assignment["command"] = ["agent", "--token", secret, "--print"]
                path.write_text(json.dumps(assignment))

            destination = root / "v2-bundle"
            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            public = b"".join(
                path.read_bytes() for path in destination.rglob("*") if path.is_file()
            )
            self.assertNotIn(secret.encode(), public)
            assignment = json.loads(
                (destination / "artifacts/assignment.json").read_text()
            )
            self.assertEqual(
                assignment["command"],
                ["agent", "--token", "[redacted-secret]", "--print"],
            )

    def test_v2_assigns_globally_unique_public_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            attempt = source / "coordinator/01-attempt"
            output_path = attempt / "output.json"
            output = json.loads(output_path.read_text())
            output["artifacts"] = {
                "events": "output.json",
                "stderr": "output.json.json",
            }
            output_path.write_text(json.dumps(output))
            (attempt / "output.json.json").write_text("ordinary log\n")

            destination = root / "v2-bundle"
            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            paths = [
                item["path"]
                for item in record["artifacts"]
                if item["state"] == "published"
            ]
            self.assertEqual(len(paths), len(set(paths)))
            manifest = json.loads((destination / "manifest.json").read_text())
            manifest_paths = [item["path"] for item in manifest["files"]]
            self.assertEqual(len(manifest_paths), len(set(manifest_paths)))

    def test_v2_seals_an_artifact_that_becomes_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "events.jsonl"
            artifact.write_text('{"type":"message_end"}\n')
            candidate = {
                "root": root,
                "source": "events.jsonl",
                "scope": "component:1:attempt",
                "kind": "events",
                "media_type": "application/x-ndjson",
            }

            with mock.patch(
                "afk_export.read_bytes",
                side_effect=PermissionError("artifact replaced after lstat"),
            ):
                descriptor, public = afk_export.derive_public_artifact(candidate, set())

            self.assertIsNone(public)
            self.assertEqual(descriptor["state"], "unavailable")
            self.assertEqual(descriptor["unavailable_reason"], "unavailable")
            self.assertEqual(descriptor["public_bytes"], 0)
            self.assertNotIn("path", descriptor)

    def test_v2_seals_an_artifact_replaced_between_lstat_and_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "events.jsonl"
            original = b'{"type":"message_end","text":"original"}\n'
            replacement = b'{"type":"message_end","text":"replacement"}\n'
            artifact.write_bytes(original)
            candidate = {
                "root": root,
                "source": "events.jsonl",
                "scope": "component:1:attempt",
                "kind": "events",
                "media_type": "application/x-ndjson",
            }
            real_read_bytes = afk_export.read_bytes

            def replace_then_read(path, limit, expected_facts=None):
                replacement_path = root / "replacement.jsonl"
                replacement_path.write_bytes(replacement)
                os.replace(replacement_path, artifact)
                return real_read_bytes(path, limit, expected_facts=expected_facts)

            with mock.patch("afk_export.read_bytes", side_effect=replace_then_read):
                descriptor, public = afk_export.derive_public_artifact(candidate, set())

            self.assertIsNone(public)
            self.assertEqual(descriptor["state"], "unavailable")
            self.assertEqual(descriptor["unavailable_reason"], "unavailable")
            self.assertEqual(descriptor["public_bytes"], 0)
            self.assertNotIn("path", descriptor)
            self.assertEqual(artifact.read_bytes(), replacement)

    def test_v2_rejects_unsafe_component_artifact_paths_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            attempt = source / "coordinator/01-attempt"
            output_path = attempt / "output.json"
            output = json.loads(output_path.read_text())
            output["artifacts"]["events"] = "declared-stderr"
            output["artifacts"]["stderr"] = "private/notes.txt"
            output_path.write_text(json.dumps(output))
            (attempt / "declared-stderr").write_text('{"type":"message_end"}\n')
            (attempt / "private").mkdir()
            secret = b"private component material\n"
            (attempt / "private/notes.txt").write_bytes(secret)
            destination = root / "v2-bundle"

            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            descriptors = [
                item
                for item in record["artifacts"]
                if item["source"]["path"] == "coordinator/01-attempt/declared-stderr"
            ]
            self.assertEqual(len(descriptors), 2)
            rejected = next(item for item in descriptors if item["kind"] == "log")
            published = next(item for item in descriptors if item["kind"] == "events")
            self.assertEqual(rejected["state"], "unavailable")
            self.assertEqual(rejected["unavailable_reason"], "unsafe_path")
            self.assertEqual(published["state"], "published")
            public = b"".join(
                path.read_bytes() for path in destination.rglob("*") if path.is_file()
            )
            self.assertNotIn(secret.rstrip(), public)

    def test_v2_rejects_a_symlinked_preflight_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            external = root / "external-preflight"
            self.add_preflight(source, external)
            (source / "preflight").symlink_to(external, target_is_directory=True)

            destination = root / "v2-bundle"
            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(json.loads(result.stdout)["error"], "invalid_run")

    def test_v2_rejects_successful_preflight_evidence_for_another_bead(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            self.add_preflight(
                source, source / "preflight", bead_id="central-unrelated"
            )

            destination = root / "v2-bundle"
            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(json.loads(result.stdout)["error"], "invalid_run")

    def test_v2_applies_the_artifact_limit_after_canonicalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            events = source / "coordinator/01-attempt/events.jsonl"
            # ensure_ascii expansion makes this public JSONL larger than 25 MiB
            # even though the retained UTF-8 source is well below that limit.
            events.write_text(
                json.dumps({"text": "é" * (9 * 1024 * 1024)}, ensure_ascii=False) + "\n"
            )
            destination = root / "v2-bundle"

            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            descriptors = {item["source"]["path"]: item for item in record["artifacts"]}
            rejected = descriptors["coordinator/01-attempt/events.jsonl"]
            self.assertEqual(rejected["state"], "unavailable")
            self.assertEqual(rejected["unavailable_reason"], "oversized")
            self.assertEqual(rejected["public_bytes"], 0)

    def test_v2_malformed_preparation_is_a_normal_invalid_run_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            (source / "preparation.json").write_text("[]")
            destination = root / "v2-bundle"

            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(json.loads(result.stdout)["error"], "invalid_run")
            self.assertNotIn("Traceback", result.stderr)

    def test_v2_exports_a_terminal_preflight_pause_without_coordinator_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.sealed_preparer(root)
            preparation_path = source / "preparation.json"
            preparation = json.loads(preparation_path.read_text())
            preflight_input = {
                "schema_version": 1,
                "source": {"kind": "bead", "id": "central-example"},
                "title": "Needs an operator",
                "acceptance_criteria": "Deployment verification passes.",
                "evidence_catalog": [
                    {
                        "category": "operator_external",
                        "route": "operator handoff",
                        "can_prove": "external deployment state",
                    }
                ],
                "timeout_seconds": 60,
            }
            request = {
                "index": 1,
                "request": "Deployment verification passes.",
                "category": "operator_external",
                "route": "operator handoff",
                "rationale": "An operator owns deployment.",
            }
            preflight_output = {
                "schema_version": 1,
                "outcome": "completed",
                "source": preflight_input["source"],
                "decision": "pause",
                "started_at": "2026-08-19T00:00:00Z",
                "finished_at": "2026-08-19T00:00:01Z",
                "duration_seconds": 1,
                "process": {"exit_code": 0, "signal": None},
                "agent": {"status": "completed"},
                "classifier": {
                    "kind": "inference",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "status": "completed",
                },
                "requests": [request],
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            }
            preparation["preparation_status"] = "paused"
            preparation["preflight"] = {
                "command": ["private"],
                "directory": "preflight",
                "result": "preflight/output.json",
                "status": "completed",
                "exit_code": 0,
                "outcome": "completed",
                "decision": "pause",
            }
            preparation["coordinator"].update(
                status="not_started", exit_code=None, outcome=None, decision=None
            )
            preparation_path.write_text(json.dumps(preparation))
            (source / "preflight-input.json").write_text(json.dumps(preflight_input))
            preflight = source / "preflight"
            preflight.mkdir()
            (preflight / "input.json").write_text(json.dumps(preflight_input))
            (preflight / "output.json").write_text(json.dumps(preflight_output))
            (preflight / "events.jsonl").write_text('{"type":"message_end"}\n')
            (preflight / "stderr.log").write_text("")
            for child in list((source / "coordinator").iterdir()):
                if child.is_dir():
                    import shutil

                    shutil.rmtree(child)
                else:
                    child.unlink()

            destination = root / "paused-bundle"
            result = self.export_v2(source, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads((destination / "workflow-run.json").read_text())
            self.assertEqual(record["status"], "paused")
            self.assertEqual(record["history"], [])
            self.assertEqual(
                record["terminal"], {"stage": "preflight", "decision": "pause"}
            )
            self.assertEqual(record["preflight"]["requests"], [request])

            invocation_input = {**preflight_input, "title": "Fabricated invocation"}
            (preflight / "input.json").write_text(json.dumps(invocation_input))
            rejected_destination = root / "inconsistent-paused-bundle"
            rejected = self.export_v2(source, rejected_destination)
            self.assertEqual(rejected.returncode, 1, rejected.stderr)
            self.assertFalse(rejected_destination.exists())
            self.assertEqual(json.loads(rejected.stdout)["error"], "invalid_run")

    def test_help_documents_the_export_interface(self):
        result = subprocess.run(
            [str(ROOT / "afk"), "export", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("afk export", result.stdout)
        self.assertIn("--project", result.stdout)

    def export(self, source, destination, *arguments):
        return subprocess.run(
            [str(ROOT / "afk"), "export", str(source), str(destination), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def export_v2(self, source, destination, *arguments):
        return subprocess.run(
            [
                str(ROOT / "afk"),
                "export",
                str(source),
                str(destination),
                "--schema-version",
                "2",
                *arguments,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def add_preflight(self, source, invocation, bead_id="central-example"):
        preflight_input = {
            "schema_version": 1,
            "source": {"kind": "bead", "id": bead_id},
            "title": "Portable publication",
            "acceptance_criteria": "The export is safe.",
            "evidence_catalog": [],
            "timeout_seconds": 60,
        }
        preflight_output = {
            "schema_version": 1,
            "outcome": "completed",
            "source": preflight_input["source"],
            "decision": "proceed",
            "started_at": "2026-08-19T00:00:00Z",
            "finished_at": "2026-08-19T00:00:01Z",
            "duration_seconds": 1,
            "process": {"exit_code": 0, "signal": None},
            "agent": {"status": "completed"},
            "classifier": {
                "kind": "inference",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "status": "completed",
            },
            "requests": [],
            "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
        }
        preparation_path = source / "preparation.json"
        preparation = json.loads(preparation_path.read_text())
        preparation["preflight"] = {
            "command": ["private"],
            "directory": "preflight",
            "result": "preflight/output.json",
            "status": "completed",
            "exit_code": 0,
            "outcome": "completed",
            "decision": "proceed",
        }
        preparation_path.write_text(json.dumps(preparation))
        (source / "preflight-input.json").write_text(json.dumps(preflight_input))
        invocation.mkdir()
        (invocation / "input.json").write_text(json.dumps(preflight_input))
        (invocation / "output.json").write_text(json.dumps(preflight_output))
        (invocation / "events.jsonl").write_text('{"type":"message_end"}\n')
        (invocation / "stderr.log").write_text("")

    def sealed_preparer(self, root):
        source = root / "source-run"
        coordinator = source / "coordinator"
        coordinator.mkdir(parents=True)
        workspace = root / "workspace"
        assignment = {
            "schema_version": 1,
            "objective": "Publish one portable Workflow Run.",
            "workspace": str(workspace),
            "command": ["agent", "--token", "should-not-export"],
            "timeout_seconds": 60,
            "source": {"kind": "bead", "id": "central-example"},
        }
        request = {
            "schema_version": 1,
            "assignment_path": str(source / "assignment.json"),
            "validation": {"command": ["./scripts/validate"], "timeout_seconds": 60},
            "agent_timeout_seconds": 60,
            "max_responses": 1,
        }
        history = self.history()
        terminal = {"decision": "stop"}
        state = {
            "schema_version": 1,
            "status": "completed",
            "next_sequence": 7,
            "next_component": None,
            "active_invocation": None,
            "history": history,
            "terminal": terminal,
        }
        output = {
            "schema_version": 1,
            "outcome": "completed",
            **terminal,
            "history": history,
        }
        preparation = {
            "schema_version": 1,
            "run": {"id": "run-example", "artifact_root": str(source)},
            "bead": {"id": "central-example"},
            "project": {"slug": "operations-webui"},
            "repository": {
                "path": str(root / "repository"),
                "base_ref": "main",
                "base_commit": "a" * 40,
                "branch": "afk-central-example-run-example",
                "worktree": str(workspace),
            },
            "timestamps": {
                "started_at": "2026-08-19T00:00:00Z",
                "prepared_at": "2026-08-19T00:00:01Z",
                "finished_at": "2026-08-19T00:01:00Z",
            },
            "preparation_status": "prepared",
            "coordinator": {
                "command": [
                    "python3",
                    "-m",
                    "afk_coordinate",
                    str(source / "coordinator-request.json"),
                    str(coordinator),
                ],
                "directory": "coordinator",
                "result": "coordinator/output.json",
                "status": "completed",
                "exit_code": 0,
                "outcome": "completed",
                "decision": "stop",
            },
            "errors": [],
        }
        for path, value in {
            source / "assignment.json": assignment,
            source / "coordinator-request.json": request,
            source / "preparation.json": preparation,
            coordinator / "assignment.json": assignment,
            coordinator / "input.json": request,
            coordinator / "state.json": state,
            coordinator / "output.json": output,
        }.items():
            path.write_text(json.dumps(value))
        self.component_outputs(coordinator)
        return source

    def history(self):
        return [
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
                "input_from": {"workspace": "assignment.json", "change": "01-attempt"},
                "outcome": "passed",
            },
            {
                "sequence": 3,
                "component": "change",
                "directory": "03-change",
                "input_from": {"source": "01-attempt"},
                "outcome": "completed",
            },
            {
                "sequence": 4,
                "component": "review",
                "directory": "04-review",
                "input_from": {"change": "03-change", "validation": "02-validation"},
                "outcome": "completed",
            },
            {
                "sequence": 5,
                "component": "assessment",
                "directory": "05-assessment",
                "input_from": {"review": "04-review"},
                "outcome": "completed",
            },
            {
                "sequence": 6,
                "component": "iteration",
                "directory": "06-iteration",
                "input_from": {"assessment": "05-assessment"},
                "outcome": "completed",
            },
        ]

    def component_outputs(self, coordinator):
        outputs = {
            "01-attempt": {
                "schema_version": 1,
                "outcome": "succeeded",
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            },
            "02-validation": {
                "schema_version": 1,
                "outcome": "passed",
                "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
            },
            "03-change": {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "workspace": str(coordinator.parent / "workspace"),
                    "objective": "portable",
                    "source": {
                        "kind": "attempt",
                        "directory": str(coordinator / "01-attempt"),
                    },
                    "repository": {},
                },
            },
            "04-review": {
                "schema_version": 1,
                "outcome": "completed",
                "review": {
                    "summary": "One finding.",
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "Example",
                            "details": "Fix it.",
                            "locations": [{"path": "README.md", "line": 1}],
                        }
                    ],
                },
                "artifacts": {
                    "diff": "review.diff",
                    "events": "events.jsonl",
                    "stderr": "stderr.log",
                },
            },
            "05-assessment": {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {
                    "summary": "Address it.",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "worth_addressing": True,
                            "rationale": "Relevant.",
                        }
                    ],
                },
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            },
            "06-iteration": {
                "schema_version": 1,
                "outcome": "completed",
                "policy": {
                    "decision": "stop",
                    "completed_responses": 0,
                    "max_responses": 1,
                    "actionable_findings": 1,
                    "reason": "Ready.",
                },
            },
        }
        events = '{"type":"agent_end"}\n{"type":"message_end"}\n'
        for directory, output in outputs.items():
            result = coordinator / directory
            result.mkdir()
            (result / "output.json").write_text(json.dumps(output))
            for kind, artifact in output.get("artifacts", {}).items():
                value = ""
                if kind == "events":
                    value = events
                elif kind == "stdout":
                    value = "validation passed\n"
                elif kind == "diff":
                    value = "diff --git a/README.md b/README.md\n"
                (result / artifact).write_text(value)


if __name__ == "__main__":
    unittest.main()
