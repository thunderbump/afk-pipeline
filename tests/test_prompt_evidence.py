import unittest

from afk_inference.runtime import PiAdapter


class PromptEvidenceTest(unittest.TestCase):
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
