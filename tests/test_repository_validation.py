"""Exercise pinned repository checks through real fixture execution."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_orchestrate import driver
from afk_pr import jobs
from tests.test_orchestrate import World

ROOT = Path(__file__).resolve().parent.parent


class RepositoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "scripts").mkdir()
        (self.source / "tests").mkdir()
        shutil.copy2(ROOT / ".pre-commit-config.yaml", self.source)
        shutil.copy2(ROOT / "scripts/validate", self.source / "scripts/validate")
        (self.source / "tests/__init__.py").write_text("")
        (self.source / "tests/test_smoke.py").write_text(
            "import unittest\n\nfrom style import value\n\n\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_value(self):\n"
            '        self.assertEqual(value, {"a": 1})\n'
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.directory = self.root / "job"
        self.directory.mkdir()

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.source), *arguments],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def candidate(self, text):
        (self.source / "style.py").write_text(text)
        self.git("add", ".")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-m", "candidate")
        head = self.git("rev-parse", "HEAD")
        self.job = {
            "layout": "independent-clones-v1",
            "repository": str(self.source),
            "head": head,
            "validation": {"command": ["./scripts/validate"], "timeout_seconds": 60},
        }
        return head

    def fixture(self):
        # Acquisition alone is adapted; execute the real command and integrity check.
        with mock.patch.object(jobs, "checkout", return_value=self.source):
            return jobs.fixtures(self.directory, self.job)

    def creation_observation(self, head, result, max_repairs=2):
        world = World()
        world.head = head
        world.creation["phases"]["creation"]["progress"]["candidate"] = head
        child = world.jobs["f" * 16]
        child["job"]["head"] = head
        child["phases"]["fixtures"] = {**result, "publication": "published"}
        path, _ = driver.create(
            self.root / f"orchestrations-{max_repairs}",
            "central-example",
            self.root / "config.toml",
            max_repairs,
        )
        driver.advance(path, world)
        return path, world, driver.advance(path, world)

    def test_unformatted_candidate_is_unchanged_and_enters_bounded_repair(self):
        head = self.candidate('value={"a":1}\n')
        before = (self.source / "style.py").read_bytes()
        result = self.fixture()
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["process"]["exit_code"], 1, result)
        self.assertTrue(result["candidate_unchanged"], result)
        self.assertEqual((self.source / "style.py").read_bytes(), before)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertIn(
            "would be reformatted", (self.directory / "fixtures.stdout.log").read_text()
        )
        path, world, state = self.creation_observation(head, result)
        self.assertEqual((state["stage"], state["repairs"]), ("response_submit", 1))
        self.assertEqual(state["head"], head)
        self.assertEqual(driver.advance(path, world)["stage"], "response_wait")
        self.assertEqual(sum(call[0] == "respond" for call in world.calls), 1)
        _, capped_world, capped = self.creation_observation(head, result, max_repairs=0)
        self.assertEqual(capped["reason"], "repair_limit_reached")
        self.assertEqual(capped["repairs"], 0)
        self.assertFalse(any(call[0] == "respond" for call in capped_world.calls))

    def test_formatted_candidate_passes_without_mutation(self):
        head = self.candidate('value = {"a": 1}\n')
        result = self.fixture()
        self.assertEqual(result["state"], "passed", result)
        self.assertEqual(result["process"]["exit_code"], 0, result)
        self.assertTrue(result["candidate_unchanged"], result)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        _, _, state = self.creation_observation(head, result)
        self.assertEqual((state["stage"], state["repairs"]), ("review_submit", 0))

    def test_mutating_validator_still_pauses_without_repair(self):
        head = self.candidate('value = {"a": 1}\n')
        self.job["validation"]["command"] = [
            "sh",
            "-c",
            "printf '\n# changed by validation\n' >> style.py; exit 1",
        ]
        result = self.fixture()
        self.assertEqual(result["process"]["exit_code"], 1, result)
        self.assertFalse(result["candidate_unchanged"], result)
        _, world, state = self.creation_observation(head, result)
        self.assertEqual((state["status"], state["repairs"]), ("paused", 0))
        self.assertFalse(any(call[0] == "respond" for call in world.calls))
