import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import afk_attest
from tests import test_completion_cli as completion_helpers

ROOT = Path(__file__).parents[1]


class AttestCliTest(unittest.TestCase):
    publish_plan = completion_helpers.CompletionRecordCliTest.publish_plan
    human_record = completion_helpers.CompletionRecordCliTest.human_record
    input_value = completion_helpers.CompletionRecordCliTest.input_value
    write_input = completion_helpers.CompletionRecordCliTest.write_input

    def setUp(self):
        completion_helpers.CompletionRecordCliTest.setUp(self)
        publisher_input = json.loads((self.publication / "input.json").read_text())
        self.beads_command = publisher_input["command"]
        self.beads = Path(publisher_input["beads_workspace"])
        self.state = self.beads / "state.json"
        self.results = self.root / "attestations"
        self.results.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "beads_workspace": str(self.beads),
                    "attestation": {"result_root": str(self.results)},
                }
            )
        )
        state = json.loads(self.state.read_text())
        state["children"][0]["status"] = "closed"
        self.state.write_text(json.dumps(state))

    def command(self, *extra):
        return [
            str(ROOT / "afk"),
            "attest",
            self.child_id,
            "--publication",
            str(self.publication),
            "--subject",
            "commit=abc123",
            "--subject",
            "environment=local production",
            "--evidence",
            "bead-comment:central-example#approval-1",
            "--config",
            str(self.config),
            *extra,
        ]

    def invoke(self, *extra, input_text=None):
        environment = os.environ.copy()
        environment["AFK_ATTEST_BEADS_COMMAND"] = json.dumps(self.beads_command)
        return subprocess.run(
            self.command(*extra),
            cwd="/",
            env=environment,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def attestation_scope(self):
        arguments = SimpleNamespace(
            child_id=self.child_id,
            publication=self.publication,
            subject=["commit=abc123", "environment=local production"],
            evidence=["bead-comment:central-example#approval-1"],
            config=self.config,
        )
        return afk_attest.load_scope(arguments), arguments.evidence

    def open_attempt(self):
        scope, evidence = self.attestation_scope()
        attempt, _, descriptor = afk_attest.open_attempt(scope, evidence)
        os.close(descriptor)
        return attempt

    def test_preview_and_decline_do_not_create_evidence_or_read_beads(self):
        before = self.state.read_text()
        arguments = SimpleNamespace(
            child_id=self.child_id,
            publication=self.publication,
            subject=["commit=abc123", "environment=local production"],
            evidence=["bead-comment:central-example#approval-1"],
            config=self.config,
            accept=False,
        )
        stdout = StringIO()
        with (
            mock.patch.object(afk_attest.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="no"),
            redirect_stdout(stdout),
        ):
            completed = afk_attest.attest(arguments)

        self.assertEqual(completed, 1)
        for value in (
            self.request["parent"]["id"],
            self.plan["plan_sha256"],
            self.child_id,
            "Brian",
            "criterion-2",
            '"commit": "abc123"',
            "bead-comment:central-example#approval-1",
        ):
            self.assertIn(value, stdout.getvalue())
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual(list(self.results.iterdir()), [])

    def test_explicit_approval_attaches_record_and_closes_only_child(self):
        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["parent"]["status"], "in_progress")
        self.assertEqual(state["children"][0]["status"], "closed")
        self.assertEqual(state["children"][1]["status"], "closed")
        record = json.loads(state["children"][1]["comments"][0])
        self.assertEqual(record["child"], self.child_id)
        self.assertEqual(
            record["producer"], {"kind": "human_attestation", "identity": "Brian"}
        )
        result = next(self.results.iterdir())
        self.assertEqual(
            json.loads((result / "output.json").read_text())["decision"], "attested"
        )

        replay = self.invoke("--accept")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["children"][1]["comments"]), 1)

    def test_interactive_terminal_approval_attaches_record(self):
        arguments = SimpleNamespace(
            child_id=self.child_id,
            publication=self.publication,
            subject=["commit=abc123", "environment=local production"],
            evidence=["bead-comment:central-example#approval-1"],
            config=self.config,
            accept=False,
        )
        with (
            mock.patch.object(afk_attest.sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="yes"),
            mock.patch.dict(
                os.environ,
                {"AFK_ATTEST_BEADS_COMMAND": json.dumps(self.beads_command)},
            ),
        ):
            completed = afk_attest.attest(arguments)

        self.assertEqual(completed, 0)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "closed")

    def test_stale_child_seals_inspectable_failure_and_leaves_child_open(self):
        state = json.loads(self.state.read_text())
        state["children"][1]["title"] = "stale published child"
        self.state.write_text(json.dumps(state))

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "open")
        result = next(self.results.iterdir())
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["error_category"], "current_state")

    def test_extra_plan_controlled_dependency_relation_is_rejected(self):
        state = json.loads(self.state.read_text())
        state["children"][1]["dependencies"].append(
            {"id": state["parent"]["id"], "dependency_type": "blocks"}
        )
        self.state.write_text(json.dumps(state))

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "open")

    def test_malformed_top_level_config_exits_without_traceback(self):
        self.config.write_text("[]")

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)

    def test_explicit_approval_succeeds_without_confirmation_input(self):
        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "closed")

    def test_retry_initializes_an_attempt_interrupted_before_request_was_sealed(self):
        attempt = self.open_attempt()
        (attempt / "request.json").unlink()

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((attempt / "request.json").is_file())
        self.assertEqual(
            json.loads((attempt / "output.json").read_text())["decision"], "attested"
        )

    def test_distinct_attestations_serialize_reconciliation(self):
        scope, _ = self.attestation_scope()
        common = {
            "child_id": self.child_id,
            "publication": self.publication,
            "subject": ["commit=abc123", "environment=local production"],
            "config": self.config,
            "accept": True,
        }
        first_arguments = SimpleNamespace(
            **common, evidence=["bead-comment:central-example#approval-1"]
        )
        second_arguments = SimpleNamespace(
            **common, evidence=["bead-comment:central-example#approval-2"]
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        results = []

        def observe_reconciliation(
            _scope, record, _attempt, _attempt_descriptor, _adapter
        ):
            if record["evidence"] == first_arguments.evidence:
                first_entered.set()
                release_first.wait(timeout=2)
            else:
                second_entered.set()

        with (
            mock.patch.object(afk_attest, "load_scope", return_value=scope),
            mock.patch.object(afk_attest, "Beads", return_value=object()),
            mock.patch.object(
                afk_attest, "reconcile", side_effect=observe_reconciliation
            ),
            mock.patch.object(afk_attest, "finish"),
        ):
            first = threading.Thread(
                target=lambda: results.append(afk_attest.attest(first_arguments))
            )
            first.start()
            self.assertTrue(first_entered.wait(timeout=2))
            second = threading.Thread(
                target=lambda: results.append(afk_attest.attest(second_arguments))
            )
            second.start()
            time.sleep(0.1)
            self.assertFalse(second_entered.is_set())
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(results, [0, 0])

    def test_retry_replaces_an_unsealed_completion_result(self):
        attempt = self.open_attempt()
        completion = attempt / "completion"
        completion.mkdir()
        (completion / "input.json").write_text('{"partial":')

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads((completion / "output.json").read_text())["outcome"],
            "completed",
        )
        abandoned = list(attempt.glob("completion.abandoned-*"))
        self.assertEqual(len(abandoned), 1)
        self.assertEqual((abandoned[0] / "input.json").read_text(), '{"partial":')

    def test_retry_reconciles_attachment_after_interrupted_close(self):
        state = json.loads(self.state.read_text())
        state["fail_next_close"] = True
        self.state.write_text(json.dumps(state))

        failed = self.invoke("--accept")
        self.assertEqual(failed.returncode, 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "open")
        self.assertEqual(len(state["children"][1]["comments"]), 1)

        retried = self.invoke("--accept")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "closed")
        self.assertEqual(len(state["children"][1]["comments"]), 1)

    def test_noninteractive_confirmation_is_required(self):
        with mock.patch.dict(os.environ, {"AFK_UNUSED": "1"}):
            completed = self.invoke(input_text="yes\n")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(self.results.iterdir()), [])

    def test_publisher_command_is_not_used_for_beads_mutation(self):
        marker = self.root / "publisher-command-ran"
        publisher_input_path = self.publication / "input.json"
        publisher_input = json.loads(publisher_input_path.read_text())
        publisher_input["command"] = [
            "/bin/sh",
            "-c",
            f"touch {marker}",
        ]
        publisher_input_path.write_text(json.dumps(publisher_input))

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists())

    def test_invalid_trusted_adapter_configuration_seals_empty_logs(self):
        environment = os.environ.copy()
        environment["AFK_ATTEST_BEADS_COMMAND"] = "not-json"

        completed = subprocess.run(
            self.command("--accept"),
            cwd="/",
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        attempt = next(self.results.iterdir())
        self.assertEqual(json.loads((attempt / "stdout.log.json").read_text()), [])
        self.assertEqual(json.loads((attempt / "stderr.log.json").read_text()), [])

    def test_attempt_symlink_cannot_redirect_durable_output(self):
        attempt = self.open_attempt()
        shutil.rmtree(attempt)
        outside = self.root / "outside"
        outside.mkdir()
        attempt.symlink_to(outside, target_is_directory=True)

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_nested_artifact_symlink_cannot_redirect_durable_output(self):
        attempt = self.open_attempt()
        (attempt / "request.json").unlink()
        outside = self.root / "outside.json"
        outside.write_text("untouched")
        (attempt / "request.json.tmp").symlink_to(outside)

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(outside.read_text(), "untouched")

    def test_nested_artifact_hard_link_cannot_redirect_durable_output(self):
        attempt = self.open_attempt()
        (attempt / "request.json").unlink()
        outside = self.root / "outside.json"
        outside.write_text("untouched")
        os.link(outside, attempt / "request.json.tmp")

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(outside.read_text(), "untouched")

    def test_lost_close_response_reconciles_observed_success(self):
        state = json.loads(self.state.read_text())
        state["close_then_fail"] = True
        self.state.write_text(json.dumps(state))

        completed = self.invoke("--accept")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["children"][1]["status"], "closed")
        result = next(self.results.iterdir())
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "attested")

    def test_successful_replay_does_not_replace_result_after_parent_closes(self):
        completed = self.invoke("--accept")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        attempt = next(self.results.iterdir())
        sealed = (attempt / "output.json").read_bytes()
        state = json.loads(self.state.read_text())
        state["parent"]["status"] = "closed"
        self.state.write_text(json.dumps(state))

        replay = self.invoke("--accept")

        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual((attempt / "output.json").read_bytes(), sealed)


if __name__ == "__main__":
    unittest.main()
