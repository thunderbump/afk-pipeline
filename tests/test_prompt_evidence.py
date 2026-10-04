import json
import tempfile
import unittest
from pathlib import Path

from afk_inference.runtime import PiAdapter
from afk_prompt_evidence import text_evidence


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
