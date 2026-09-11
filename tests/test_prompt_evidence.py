import json
import tempfile
import unittest
from pathlib import Path

from afk_inference.runtime import PiAdapter
from afk_prompt_evidence import text_evidence
from afk_respond.task import build_task


class PromptEvidenceTest(unittest.TestCase):
    def test_small_text_inline_large_and_escaped_text_referenced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log"
            path.write_text("small log")
            self.assertEqual(text_evidence(path), "small log")
            path.write_bytes(b"\0" * 4096)
            value = text_evidence(path)
            self.assertEqual(value["path"], str(path))
            self.assertEqual(value["bytes"], 4096)
            self.assertLess(len(json.dumps(value)), 1024)

    def test_repair_references_large_logs_and_selected_feedback_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "input.json").write_text("{}")
            (directory / "output.json").write_text("{}")
            (directory / "stdout.log").write_text("failure detail\n" * 4096)
            (directory / "stderr.log").write_text("")
            task = build_task(
                {"validation_directory": str(directory)}, [], "Fix validation"
            )
            reference = task.untrusted_data["failed_validation"]["stdout"]
            self.assertIn(reference["path"], task.read_only_evidence)
            self.assertLess(len(json.dumps(task.untrusted_data)), 4096)
            task = build_task(
                {}, [{"details": "x" * 5000}], "Fix the owned defect", directory
            )
            packet = task.untrusted_data["task_data"]
            self.assertIn(packet["path"], task.read_only_evidence)
            self.assertEqual(
                json.loads(Path(packet["path"]).read_text())["objective"],
                "Fix the owned defect",
            )

    def test_pi_refuses_large_inline_data_before_rendering(self):
        prompt = {
            "system": "test",
            "trusted_task_instructions": "test",
            "purpose": "review",
            "task_contract_version": 10,
            "untrusted_task_data": {"log": "x" * 65536},
        }
        with self.assertRaisesRegex(ValueError, "reference large evidence"):
            PiAdapter(model="test", thinking="high").render(prompt)
