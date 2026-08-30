import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_inference import Capability
from afk_parent_review import __main__ as parent_review_cli
from afk_plan import __main__ as planner_cli
from tests import test_parent_review_cli
from tests.test_pi_inference_adapter import FakeProcess


def direct_proposal():
    return {
        "schema_version": 1,
        "decision": "direct",
        "criteria": [
            {
                "id": "criterion-1",
                "source_text": "The change is implemented and tested.",
                "statement": "Implement and test the change.",
            }
        ],
        "direct_routes": [
            {
                "criterion": "criterion-1",
                "project": "afk-pipeline",
                "owner": "AFK implementation agent",
                "phase": "implementation",
                "execution": "agent",
                "evidence_route": "pipeline_run",
            }
        ],
        "children": [],
        "ambiguities": [],
    }


class NoToolRuntimeRoleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "planner.json"
        self.result = self.root / "result"
        self.request = {
            "schema_version": 1,
            "parent": {
                "id": "central-example",
                "title": "Route work",
                "description": "A domain value, not an instruction.",
                "acceptance_criteria": "The change is implemented and tested.",
                "labels": ["project:afk-pipeline"],
            },
            "catalog": {
                "schema_version": 1,
                "projects": [
                    {
                        "slug": "afk-pipeline",
                        "routes": [
                            {
                                "owner": "AFK implementation agent",
                                "execution": "agent",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation"],
                            }
                        ],
                    }
                ],
            },
            "timeout_seconds": 5,
        }

    def pi_process(self, response):
        events = (
            '{"type":"agent_start"}\n'
            + json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": response}],
                    },
                }
            )
            + '\n{"type":"agent_end"}\n'
        )
        return FakeProcess(events.encode())

    def invoke(self, response):
        self.input.write_text(json.dumps(self.request))
        argv = ["afk_plan", str(self.input), str(self.result)]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "afk_inference.runtime.subprocess.Popen",
                return_value=self.pi_process(response),
            ) as popen,
        ):
            code = planner_cli.main()
        return code, popen

    def test_planner_uses_semantic_no_tools_and_retains_receipt(self):
        code, popen = self.invoke(json.dumps(direct_proposal()))
        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertIn("--no-tools", command)
        self.assertNotIn("--tools", command)

        receipt = json.loads((self.result / "inference/receipt.json").read_text())
        prompt = json.loads((self.result / "inference/prompt.json").read_text())
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], Capability.NO_TOOLS)
        self.assertEqual(receipt["terminal_response"], json.dumps(direct_proposal()))
        self.assertEqual(prompt["untrusted_task_data"], self.request)
        self.assertEqual(output["outcome"], "completed")

    def test_rejected_planner_response_keeps_terminal_evidence(self):
        response = '{"not":"a proposal"}'
        code, _ = self.invoke(response)
        self.assertEqual(code, 1)
        receipt = json.loads((self.result / "inference/receipt.json").read_text())
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(receipt["outcome"], "response_rejected")
        self.assertEqual(receipt["terminal_response"], response)
        self.assertEqual(output["error_category"], "invalid_proposal")
        response_path = receipt["attempts"][0]["artifacts"]["response"]
        self.assertTrue((self.result / "inference" / response_path).is_file())

    def invoke_parent_review(self, response):
        evidence = test_parent_review_cli.ParentAcceptanceReviewCliTest(
            "test_accepts_complete_mixed_owner_fan_in"
        )
        evidence.setUp()
        self.addCleanup(evidence.doCleanups)
        evidence.input_path.write_text(json.dumps(evidence.input_value()))
        argv = ["afk_parent_review", str(evidence.input_path), str(evidence.result)]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "afk_inference.runtime.subprocess.Popen",
                return_value=self.pi_process(response),
            ) as popen,
        ):
            code = parent_review_cli.main()
        return code, evidence, popen

    def test_parent_review_uses_semantic_no_tools_runtime(self):
        response = json.dumps(
            {
                "schema_version": 1,
                "decision": "accepted",
                "criteria": [
                    {
                        "id": "criterion-1",
                        "decision": "accepted",
                        "rationale": "Complete.",
                    },
                    {
                        "id": "criterion-2",
                        "decision": "accepted",
                        "rationale": "Complete.",
                    },
                ],
                "gaps": [],
                "follow_up": None,
            }
        )
        code, evidence, popen = self.invoke_parent_review(response)
        self.assertEqual(code, 0)
        self.assertIn("--no-tools", popen.call_args.args[0])
        receipt = json.loads((evidence.result / "inference/receipt.json").read_text())
        prompt = json.loads((evidence.result / "inference/prompt.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "NO_TOOLS")
        self.assertEqual(receipt["terminal_response"], response)
        self.assertEqual(
            prompt["untrusted_task_data"]["parent"]["id"], "central-example"
        )

    def test_rejected_parent_review_keeps_runtime_receipt_and_response(self):
        response = '{"decision":"trusted without criterion evidence"}'
        code, evidence, _ = self.invoke_parent_review(response)
        self.assertEqual(code, 1)
        receipt = json.loads((evidence.result / "inference/receipt.json").read_text())
        output = json.loads((evidence.result / "output.json").read_text())
        self.assertEqual(receipt["outcome"], "response_rejected")
        self.assertEqual(receipt["terminal_response"], response)
        self.assertEqual(output["error_category"], "invalid_review")
        response_path = receipt["attempts"][0]["artifacts"]["response"]
        self.assertTrue((evidence.result / "inference" / response_path).is_file())


if __name__ == "__main__":
    unittest.main()
