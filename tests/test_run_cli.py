import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class RunPreparerCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.repository / "README.md").write_text("fixture\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.beads = self.root / "central-beads"
        self.beads.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bead = {
            "id": "central-123",
            "title": "Change the fixture",
            "description": "Do the requested work.",
            "design": "Keep it small.",
            "acceptance_criteria": "Commit the result.",
            "labels": ["project:fixture", "priority:normal"],
        }
        self.write_bd()
        self.config = self.root / "config.json"
        self.write_config()

    def test_help_and_exactly_one_bead_id(self):
        help_result = self.invoke("--help")
        missing = self.invoke("run")
        extra = self.invoke("run", "one", "two")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("afk run <bead-id>", help_result.stdout)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(extra.returncode, 2)

    def test_prepares_from_outside_repository_and_freezes_safe_payloads(self):
        result = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertEqual(result.returncode, 1, result.stderr)
        artifact = self.artifact_from(result.stdout)
        bead = json.loads((artifact / "bead.json").read_text())
        assignment = json.loads((artifact / "assignment.json").read_text())
        request = json.loads((artifact / "coordinator-request.json").read_text())
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(bead["source"], {"kind": "bead", "id": "central-123"})
        self.assertEqual(assignment["source"], bead["source"])
        self.assertEqual(
            assignment["objective"],
            "Change the fixture\n\nDescription\nDo the requested work.\n\nDesign\nKeep it small.\n\nAcceptance criteria\nCommit the result.",
        )
        self.assertEqual(preparation["repository"]["base_commit"], self.base)
        self.assertEqual(preparation["project"]["slug"], "fixture")
        self.assertEqual(preparation["preparation_status"], "prepared")
        self.assertEqual(preparation["coordinator"]["exit_code"], 1)
        self.assertEqual(preparation["coordinator"]["outcome"], "failed")
        self.assertTrue(Path(assignment["workspace"]).is_dir())
        self.assertEqual(
            json.loads(
                (Path(assignment["workspace"]) / "worker-environment.json").read_text()
            ),
            {"unrelated": None},
        )
        self.assertEqual(request["assignment_path"], str(artifact / "assignment.json"))
        self.assertTrue((artifact / "coordinator").is_dir())
        self.assertNotIn(
            "TOP_SECRET",
            "".join(
                p.read_text(errors="replace")
                for p in artifact.rglob("*")
                if p.is_file()
            ),
        )

    def test_ownership_mapping_repository_ref_and_collision_fail_before_git_mutation(
        self,
    ):
        cases = [
            ([], "exactly one project:<slug>"),
            (["project:a", "project:b"], "exactly one project:<slug>"),
            (["project:missing"], "has no configured project mapping"),
        ]
        before = self.git("worktree", "list", "--porcelain")
        for labels, text in cases:
            self.bead["labels"] = labels
            self.write_bd()
            result = self.invoke("run", self.bead["id"], "--config", str(self.config))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(text, result.stderr)
            self.assertEqual(self.git("worktree", "list", "--porcelain"), before)
        self.bead["labels"] = ["project:fixture"]
        self.write_bd()
        config = json.loads(self.config.read_text())
        config["projects"]["fixture"]["base_ref"] = "absent"
        self.config.write_text(json.dumps(config))
        result = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertIn("base ref 'absent' is unavailable", result.stderr)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before)

    def test_invalid_repository_and_colliding_root_are_rejected(self):
        config = json.loads(self.config.read_text())
        config["projects"]["fixture"]["repository"] = str(
            self.root / "not-a-repository"
        )
        (self.root / "not-a-repository").mkdir()
        self.config.write_text(json.dumps(config))
        invalid = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertIn("not a valid repository root", invalid.stderr)

        self.write_config()
        occupied = self.root / "occupied-worktree-root"
        occupied.write_text("not a directory")
        config = json.loads(self.config.read_text())
        config["worktree_root"] = str(occupied)
        self.config.write_text(json.dumps(config))
        collision = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertNotEqual(collision.returncode, 0)
        self.assertIn("worktree_root", collision.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_failed_worktree_creation_is_rolled_back_and_sealed(self):
        git = self.bin / "git"
        git.write_text(
            "#!/usr/bin/env python3\nimport os,sys\n"
            "if sys.argv[1:3] == ['worktree', 'add']:\n raise SystemExit(9)\n"
            "os.execv('/usr/bin/git', ['git', *sys.argv[1:]])\n"
        )
        git.chmod(0o755)
        before = self.git("worktree", "list", "--porcelain")
        result = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertEqual(result.returncode, 2)
        artifact = self.artifact_from(result.stdout)
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(preparation["preparation_status"], "failed")
        self.assertEqual(preparation["errors"][0]["category"], "worktree_creation")
        self.assertTrue((artifact / "coordinator").is_dir())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), before)
        self.assertFalse(Path(preparation["repository"]["worktree"]).exists())

    def test_missing_bead_is_reported_without_leaking_reader_secret(self):
        self.write_bd(exit_code=1, stderr="TOP_SECRET database password")
        result = self.invoke("run", "central-missing", "--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("central-missing", result.stderr)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("TOP_SECRET", result.stderr + result.stdout)

    def write_config(self):
        value = {
            "schema_version": 1,
            "beads_workspace": str(self.beads),
            "run_root": str(self.root / "runs"),
            "worktree_root": str(self.root / "worktrees"),
            "assignment": {
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import json,os; from pathlib import Path; "
                        "Path('worker-environment.json').write_text(json.dumps("
                        "{'unrelated': os.getenv('UNRELATED_CANARY')})); raise SystemExit(7)"
                    ),
                ],
                "timeout_seconds": 5,
            },
            "coordinator": {"agent_timeout_seconds": 5, "max_responses": 0},
            "projects": {
                "fixture": {
                    "repository": str(self.repository),
                    "base_ref": "HEAD",
                    "validation": {
                        "command": [sys.executable, "-c", "pass"],
                        "timeout_seconds": 5,
                    },
                }
            },
        }
        self.config.write_text(json.dumps(value))

    def write_bd(self, exit_code=0, stderr=""):
        path = self.bin / "bd"
        path.write_text(
            "#!/usr/bin/env python3\nimport json,sys\n"
            + (
                f"print({stderr!r}, file=sys.stderr)\nraise SystemExit({exit_code})\n"
                if exit_code
                else f"print(json.dumps([{self.bead!r}]))\n"
            )
        )
        path.chmod(0o755)

    def invoke(self, *arguments):
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["UNRELATED_CANARY"] = "TOP_SECRET-must-not-be-forwarded"
        return subprocess.run(
            [str(ROOT / "afk"), *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact_from(self, stdout):
        line = next(
            line for line in stdout.splitlines() if line.startswith("artifact root: ")
        )
        return Path(line.removeprefix("artifact root: "))

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
