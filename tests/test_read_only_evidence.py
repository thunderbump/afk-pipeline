import json
import tempfile
import unittest
from pathlib import Path

from afk_inference import Capability, FixtureAdapter, InferenceRuntime, ScriptedResult


class ReadOnlyEvidenceTest(unittest.TestCase):
    def test_only_explicit_files_extend_read_only_authority_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            allowed = root / "diff.patch"
            allowed.write_text("diff evidence")
            other = root / "untrusted-path"
            result = InferenceRuntime().invoke(
                purpose="review",
                trusted_task_instructions="Inspect supplied evidence.",
                untrusted_task_data={"file": str(other)},
                requested_capability=Capability.READ_ONLY,
                execution_root=workspace,
                timeout_seconds=2,
                evidence_directory=root / "inference",
                adapter=FixtureAdapter((ScriptedResult(response="ok"),)),
                validator=lambda value: value,
                read_only_evidence=(str(allowed),),
            )
            self.assertEqual(result.outcome, "succeeded")
            invocation = json.loads((root / "inference/invocation.json").read_text())
            system = invocation["prompt"]["system"]
            self.assertIn(str(allowed), system)
            self.assertNotIn(str(other), system)
            self.assertEqual(result.receipt["policy"]["system_instructions"], system)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_no_tools_write_and_directory_grants_are_rejected_before_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "evidence.txt"
            file.write_text("evidence")
            for capability, path in [
                (Capability.NO_TOOLS, file),
                (Capability.WRITE, file),
                (Capability.READ_ONLY, root),
            ]:
                with self.subTest(capability=capability), self.assertRaises(ValueError):
                    InferenceRuntime().invoke(
                        purpose="test",
                        trusted_task_instructions="No extra access.",
                        untrusted_task_data={},
                        requested_capability=capability,
                        execution_root=root,
                        timeout_seconds=2,
                        evidence_directory=root / "inference",
                        adapter=FixtureAdapter((ScriptedResult(response="ok"),)),
                        validator=lambda value: value,
                        read_only_evidence=(str(path),),
                    )
            self.assertFalse((root / "inference").exists())
