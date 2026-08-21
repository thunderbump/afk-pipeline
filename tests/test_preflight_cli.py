import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_preflight_agent.py"


class PreflightCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "preflight.json"
        self.result = self.root / "result"
        self.store = self.root / "classifications"
        self.input = {
            "schema_version": 1,
            "source": {"kind": "bead", "id": "central-43zn.32"},
            "title": "Expose Coordinator terminal decision through Run Preparer",
            "acceptance_criteria": (
                "The terminal decision is recorded and tested. Validation is shared "
                "through one contract module."
            ),
            "evidence_catalog": [
                {
                    "category": "repository_validation",
                    "route": "repository validation",
                    "can_prove": "Behavior covered by the configured repository command.",
                },
                {
                    "category": "pipeline_evidence",
                    "route": "AFK committed change and Review",
                    "can_prove": "Committed code structure and review findings.",
                },
                {
                    "category": "operator_external",
                    "route": "operator handoff",
                    "can_prove": "Host, deployment, service, and HTTP behavior.",
                },
            ],
            "timeout_seconds": 5,
        }

    def test_repository_and_pipeline_requests_proceed(self):
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "proceed")
        self.assertEqual(output["source"], self.input["source"])
        self.assertEqual(
            [request["category"] for request in output["requests"]],
            ["repository_validation", "pipeline_evidence"],
        )
        self.assertEqual(output["classifier"]["kind"], "inference")
        self.assertEqual(output["classifier"]["provider"], "openai-codex")
        self.assertEqual(output["classifier"]["model"], "gpt-5.6-luna")
        self.assertEqual(output["classifier"]["status"], "completed")
        self.assertEqual(output["classifier"]["source"], "inferred")
        self.assertEqual(
            output["artifacts"], {"events": "events.jsonl", "stderr": "stderr.log"}
        )
        self.assertEqual(
            json.loads((self.result / "input.json").read_text()), self.input
        )
        self.assertTrue((self.result / "events.jsonl").is_file())
        self.assertFalse((self.result / "output.json.tmp").exists())

    def test_unchanged_input_and_policy_reuse_one_stored_classification(self):
        marker = self.root / "classifier-invocations.log"
        command = [sys.executable, str(FIXTURE), "counted-proceed", str(marker)]

        first = self.invoke("unused", command=command)
        first_output = json.loads((self.result / "output.json").read_text())
        self.result = self.root / "second-result"
        second = self.invoke("unused", command=command)
        second_output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])
        self.assertEqual(first_output["classifier"]["source"], "inferred")
        self.assertEqual(second_output["classifier"]["source"], "reused")
        self.assertEqual(
            first_output["classifier"]["key"], second_output["classifier"]["key"]
        )
        self.assertEqual(first_output["requests"], second_output["requests"])
        self.assertEqual(first_output["decision"], second_output["decision"])
        self.assertIsNone(second_output["process"]["exit_code"])
        self.assertIsNone(second_output["agent"])

    def test_policy_change_gets_a_new_key_and_fresh_classification(self):
        marker = self.root / "classifier-invocations.log"
        first = self.invoke(
            "unused",
            command=[sys.executable, str(FIXTURE), "counted-proceed", str(marker)],
        )
        first_output = json.loads((self.result / "output.json").read_text())
        self.result = self.root / "changed-policy-result"
        second = self.invoke(
            "unused",
            command=[sys.executable, str(FIXTURE), "counted-pause", str(marker)],
        )
        second_output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["invoked", "invoked"])
        self.assertNotEqual(
            first_output["classifier"]["key"], second_output["classifier"]["key"]
        )
        self.assertEqual(second_output["classifier"]["source"], "inferred")
        self.assertEqual(second_output["decision"], "pause")

    def test_corrupt_record_fails_closed_without_reclassification(self):
        marker = self.root / "classifier-invocations.log"
        command = [sys.executable, str(FIXTURE), "counted-proceed", str(marker)]
        first = self.invoke("unused", command=command)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_output = json.loads((self.result / "output.json").read_text())
        record = self.store / first_output["classifier"]["record"]
        record.write_text('{"schema_version":1,"corrupt":true}\n')
        self.result = self.root / "corrupt-reuse-result"

        second = self.invoke("unused", command=command)
        second_output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual(second_output["outcome"], "failed")
        self.assertEqual(second_output["decision"], "pause")
        self.assertEqual(second_output["classifier"]["source"], "reused")
        self.assertIn("invalid shape", second_output["classification_error"])
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])
        self.assertEqual(record.read_text(), '{"schema_version":1,"corrupt":true}\n')

    def test_invalid_stored_classification_is_not_mistaken_for_new_inference(self):
        marker = self.root / "classifier-invocations.log"
        command = [sys.executable, str(FIXTURE), "counted-proceed", str(marker)]
        first = self.invoke("unused", command=command)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_output = json.loads((self.result / "output.json").read_text())
        record = self.store / first_output["classifier"]["record"]
        stored = json.loads(record.read_text())
        stored["classification"]["requests"][0]["route"] = "invented route"
        record.write_text(json.dumps(stored))
        self.result = self.root / "invalid-classification-reuse"

        second = self.invoke("unused", command=command)
        second_output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual(second_output["classifier"]["source"], "reused")
        self.assertIsNone(second_output["process"]["exit_code"])
        self.assertIsNone(second_output["agent"])
        self.assertIn("invalid classification", second_output["classification_error"])
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])

    def test_failed_process_does_not_publish_a_reusable_classification(self):
        marker = self.root / "classifier-invocations.log"
        command = [
            sys.executable,
            str(FIXTURE),
            "counted-valid-failure",
            str(marker),
        ]

        first = self.invoke("unused", command=command)
        first_output = json.loads((self.result / "output.json").read_text())
        self.result = self.root / "retry-result"
        second = self.invoke("unused", command=command)
        second_output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["invoked", "invoked"])
        self.assertEqual(first_output["outcome"], "failed")
        self.assertEqual(second_output["outcome"], "failed")
        self.assertEqual(list(self.store.glob("*.json")), [])

    def test_oversized_classification_is_never_published_or_reused(self):
        marker = self.root / "classifier-invocations.log"
        command = [
            sys.executable,
            str(FIXTURE),
            "counted-too-many",
            str(marker),
        ]

        first = self.invoke("unused", command=command)
        first_output = json.loads((self.result / "output.json").read_text())
        self.result = self.root / "retry-result"
        second = self.invoke("unused", command=command)

        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(second.returncode, 1, second.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["invoked", "invoked"])
        self.assertIn("at most 256", first_output["classification_error"])
        self.assertEqual(list(self.store.glob("*.json")), [])

    def test_unavailable_store_seals_a_pause_without_running_inference(self):
        marker = self.root / "classifier-invocations.log"
        unavailable_parent = self.root / "not-a-directory"
        unavailable_parent.write_text("fixture\n")
        self.store = unavailable_parent / "classifications"

        completed = self.invoke(
            "unused",
            command=[sys.executable, str(FIXTURE), "counted-proceed", str(marker)],
        )
        output = json.loads((self.result / "output.json").read_text())

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["classifier"]["source"], "unavailable")
        self.assertEqual(
            output["classification_error"], "classification store unavailable"
        )
        self.assertFalse(marker.exists())

    def test_concurrent_first_calls_publish_one_classification(self):
        marker = self.root / "classifier-invocations.log"
        command = [
            sys.executable,
            str(FIXTURE),
            "counted-slow-proceed",
            str(marker),
        ]
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(command)
        results = [self.root / "concurrent-a", self.root / "concurrent-b"]
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "afk_preflight",
                    self.input_path,
                    result,
                    "--classification-store",
                    self.store,
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for result in results
        ]
        completed = [process.communicate(timeout=5) for process in processes]
        outputs = [
            json.loads((result / "output.json").read_text()) for result in results
        ]

        self.assertEqual(
            [process.returncode for process in processes], [0, 0], completed
        )
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])
        self.assertEqual(
            sorted(output["classifier"]["source"] for output in outputs),
            ["inferred", "reused"],
        )
        self.assertEqual(len({output["classifier"]["key"] for output in outputs}), 1)
        self.assertEqual(outputs[0]["requests"], outputs[1]["requests"])

    def test_interrupt_while_waiting_for_store_lock_seals_a_pause(self):
        marker = self.root / "classifier-invocations.log"
        command = [
            sys.executable,
            str(FIXTURE),
            "counted-lock-holder",
            str(marker),
        ]
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(command)
        holder_result = self.root / "lock-holder"
        waiter_result = self.root / "lock-waiter"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "afk_preflight",
                self.input_path,
                holder_result,
                "--classification-store",
                self.store,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(marker.is_file())
        waiter = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "afk_preflight",
                self.input_path,
                waiter_result,
                "--classification-store",
                self.store,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if (waiter_result / "input.json").is_file():
                break
            time.sleep(0.01)
        self.assertTrue((waiter_result / "input.json").is_file())
        time.sleep(0.05)
        waiter.send_signal(signal.SIGINT)
        _waiter_stdout, waiter_stderr = waiter.communicate(timeout=5)
        _holder_stdout, holder_stderr = holder.communicate(timeout=5)

        self.assertEqual(waiter.returncode, 1, waiter_stderr)
        self.assertEqual(holder.returncode, 0, holder_stderr)
        output = json.loads((waiter_result / "output.json").read_text())
        self.assertEqual(output["outcome"], "interrupted")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["classifier"]["source"], "unavailable")
        self.assertEqual(
            output["classification_error"], "classification store wait interrupted"
        )

    def test_operator_owned_requests_pause_before_implementation(self):
        self.input["source"]["id"] = "central-6xx4.1"
        self.input["title"] = "Register Operations WebUI as a first-class Project"
        self.input["acceptance_criteria"] = (
            "Tests, build, deployment and HTTP verification pass."
        )

        completed = self.invoke("pause")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(
            [request["category"] for request in output["requests"]],
            [
                "repository_validation",
                "operator_external",
                "operator_external",
                "operator_external",
            ],
        )
        self.assertEqual(
            [request["request"] for request in output["requests"][1:]],
            ["Build passes.", "Deployment passes.", "HTTP verification passes."],
        )

    def test_invalid_classification_fails_closed_with_a_sealed_pause(self):
        completed = self.invoke("invalid-classification")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["classifier"]["status"], "failed")
        self.assertEqual(output["requests"], [])
        self.assertIn("nonempty array", output["classification_error"])

    def test_invalid_input_and_existing_result_are_refused_without_replacement(self):
        self.input["acceptance_criteria"] = ""
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

        self.input["acceptance_criteria"] = "Commit the result."
        self.result.mkdir()
        sentinel = self.result / "keep.txt"
        sentinel.write_text("caller data\n")
        completed = self.invoke("proceed")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(self.result.iterdir()), [sentinel])

        self.result = self.root / "overlapping-result"
        self.store = self.result / "classifications"
        completed = self.invoke("proceed")
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

    def test_agent_protocol_and_launch_failures_seal_a_pause(self):
        completed = self.invoke("invalid-events")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "pause")
        self.assertEqual(output["agent"]["status"], "error")

        self.result = self.root / "launch-failure"
        completed = self.invoke("unused", command=[str(self.root / "missing-agent")])
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "pause")
        self.assertIsNone(output["agent"])
        self.assertIn("error", output["process"])
        self.assertEqual(list(self.store.glob("*.json")), [])

    def test_timeout_and_interrupt_terminate_the_classifier_process_group(self):
        marker = self.root / "timed-out-descendant.pid"
        self.input["timeout_seconds"] = 1
        completed = self.invoke(
            "hang", command=[sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        self.assertEqual(output["decision"], "pause")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

        marker = self.root / "interrupted-descendant.pid"
        self.result = self.root / "interrupted"
        self.input["timeout_seconds"] = 5
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(
            [sys.executable, str(FIXTURE), "hang", str(marker)]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "afk_preflight",
                self.input_path,
                self.result,
                "--classification-store",
                self.store,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(marker.is_file())
        process.send_signal(signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 1, stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "interrupted")
        self.assertEqual(output["decision"], "pause")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def invoke(self, scenario, command=None):
        self.input_path.write_text(json.dumps(self.input))
        environment = self.environment(
            command or [sys.executable, str(FIXTURE), scenario]
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_preflight",
                self.input_path,
                self.result,
                "--classification-store",
                self.store,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def environment(self, command):
        environment = os.environ.copy()
        environment["AFK_PREFLIGHT_AGENT_COMMAND"] = json.dumps(command)
        return environment


if __name__ == "__main__":
    unittest.main()
