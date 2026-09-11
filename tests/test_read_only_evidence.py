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

    def test_no_tools_and_directory_grants_are_rejected_before_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "evidence.txt"
            file.write_text("evidence")
            for capability, path in [
                (Capability.NO_TOOLS, file),
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

    def test_write_worker_can_read_evidence_without_external_write_authority(self):
        from afk_inference.runtime import evidence_system_instructions

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "validation.log"
            path.write_text("retained evidence")
            instructions = evidence_system_instructions(Capability.WRITE, (str(path),))
            self.assertIn("read, but never modify", instructions)
            self.assertIn("modify files only within the execution root", instructions)
            self.assertIn(str(path), instructions)
