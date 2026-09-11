"""Deterministic checks of replay fairness, provenance and runtime wiring."""

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from afk_inference import FixtureAdapter, ScriptedResult
from afk_review.contract import REVIEW_AUDIT
from afk_review.task import REVIEW_INSTRUCTIONS
from experiments.split_review import (
    ARMS,
    LENSES,
    diff,
    instructions,
    main,
    prepare_case,
    run_call,
    task_data,
)


class SplitReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "sample.txt").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        (self.repo / "sample.txt").write_text("after\n")
        self.git("commit", "-qam", "candidate")
        head = self.git("rev-parse", "HEAD")
        files = {}
        for name in ("work_diff", "repair_diff"):
            path = self.inputs / name
            raw = diff(self.repo, base, head).encode()
            path.write_bytes(raw)
            files[name] = {
                "path": str(path),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        preparation = self.inputs / "preparation.json"
        preparation.write_text(json.dumps({"repository": {"base_commit": base}}))
        invocation = self.inputs / "invocation.json"
        invocation.write_text(
            json.dumps(
                {
                    "purpose": "review",
                    "execution_root": str(self.repo),
                    "prompt": {
                        "trusted_task_instructions": REVIEW_INSTRUCTIONS,
                        "untrusted_task_data": {
                            "objective": "Change sample",
                            "related_work": [],
                            "reviewed_commits": {"before": base, "after": head},
                            "work_context": {
                                "work_base": base,
                                "candidate": head,
                                "repair_base": base,
                                "files": files,
                            },
                        },
                    },
                }
            )
        )
        self.item = {
            "id": "case",
            "preparation": str(preparation),
            "invocation": str(invocation),
            "input_hashes": {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.inputs.iterdir()
            },
        }
        self.case = prepare_case(self.repo, self.item)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True
        ).strip()

    def test_lens_split_preserves_common_contract_and_identical_data(self):
        original = copy.deepcopy(self.case["invocation"])
        data = task_data(self.case, Path("/candidate"), Path("/packet"))
        for arm in ARMS:
            prompt = instructions(self.case, arm)
            for lens, packet in LENSES.items():
                self.assertEqual(packet in prompt, arm in ("combined", lens))
            self.assertIn('"scope_claim"', prompt)
            self.assertEqual(
                data, task_data(self.case, Path("/candidate"), Path("/packet"))
            )
        self.assertEqual(self.case["invocation"], original)
        self.assertEqual(
            data["work_context"]["files"]["work_diff"]["path"], "/packet/work_diff"
        )

    def test_changed_source_is_rejected(self):
        path = Path(self.item["preparation"])
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            prepare_case(self.repo, self.item)

    def test_corrupt_packet_is_rejected(self):
        (self.inputs / "work_diff").write_text("different")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            prepare_case(self.repo, self.item)

    def test_fixture_runtime_preserves_candidate_and_evidence(self):
        output = {
            "summary": "No concrete defect.",
            "findings": [],
            "audit": REVIEW_AUDIT,
        }
        adapter = FixtureAdapter((ScriptedResult(response=json.dumps(output)),))
        record = run_call(
            self.case, "behavior", 1, self.repo, self.root / "call", adapter, 30
        )
        self.assertEqual(record["outcome"], "succeeded", record)
        self.assertTrue(record["workspace_unchanged"])
        self.assertTrue(record["packet_unchanged"])
        self.assertTrue((self.root / "call/inference/invocation.json").exists())

    def test_cli_preflight_and_four_isolated_fixture_calls(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps([{**self.item, "repo": str(self.repo)}]))
        prepared = self.root / "prepared"
        with (
            patch(
                "sys.argv", ["replay", str(manifest), str(prepared), "--prepare-only"]
            ),
            patch("experiments.split_review.PiAdapter") as pi,
        ):
            self.assertEqual(main(), 0)
            pi.assert_not_called()
        self.assertFalse((prepared / "calls").exists())
        output = {
            "summary": "No concrete defect.",
            "findings": [],
            "audit": REVIEW_AUDIT,
        }
        adapter = FixtureAdapter((ScriptedResult(response=json.dumps(output)),))
        results = self.root / "results"
        with (
            patch(
                "sys.argv",
                ["replay", str(manifest), str(results), "--repetitions", "1"],
            ),
            patch("experiments.split_review.PiAdapter", return_value=adapter),
        ):
            self.assertEqual(main(), 0)
        summary = json.loads((results / "summary.json").read_text())
        self.assertTrue(summary["retained_inputs_unchanged"])
        self.assertEqual({call["arm"] for call in summary["calls"]}, set(ARMS))
        self.assertEqual(len(list((results / "workspaces").iterdir())), 4)
