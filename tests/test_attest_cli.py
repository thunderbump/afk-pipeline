import json
import os
import subprocess
import threading
import time
import unittest
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
        return subprocess.run(
            self.command(*extra),
            cwd="/",
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
        return afk_attest.open_attempt(scope, evidence)[0]

    def test_preview_and_decline_do_not_create_evidence_or_read_beads(self):
        before = self.state.read_text()
        completed = self.invoke(input_text="no\n")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        for value in (
            self.request["parent"]["id"],
            self.plan["plan_sha256"],
            self.child_id,
            "Brian",
            "criterion-2",
            '"commit": "abc123"',
            "bead-comment:central-example#approval-1",
        ):
            self.assertIn(value, completed.stdout)
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual(list(self.results.iterdir()), [])

    def test_interactive_and_explicit_approval_attach_record_and_close_only_child(self):
        completed = self.invoke(input_text="yes\n")

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

        def observe_reconciliation(_scope, record, _attempt, _adapter):
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

    def test_concurrent_initialization_reuses_the_exclusively_sealed_request(self):
        scope, evidence = self.attestation_scope()
        real_seal = afk_attest.seal_json
        first_seal_started = threading.Event()
        release_first_seal = threading.Event()
        seal_count = 0
        seal_count_lock = threading.Lock()
        records = []
        errors = []

        def delayed_seal(path, value):
            nonlocal seal_count
            if path.name == "request.json":
                with seal_count_lock:
                    seal_count += 1
                    current = seal_count
                if current == 1:
                    first_seal_started.set()
                    release_first_seal.wait(timeout=2)
            real_seal(path, value)

        def initialize():
            try:
                records.append(afk_attest.open_attempt(scope, evidence)[1])
            except (afk_attest.AttestationError, OSError, StopIteration) as error:
                errors.append(error)

        with (
            mock.patch.object(afk_attest, "seal_json", side_effect=delayed_seal),
            mock.patch.object(
                afk_attest,
                "timestamp",
                side_effect=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"],
            ),
        ):
            first = threading.Thread(target=initialize)
            first.start()
            self.assertTrue(first_seal_started.wait(timeout=2))
            second = threading.Thread(target=initialize)
            second.start()
            time.sleep(0.1)
            release_first_seal.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])
        self.assertEqual(seal_count, 1)

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
            completed = self.invoke(input_text="")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(self.results.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
