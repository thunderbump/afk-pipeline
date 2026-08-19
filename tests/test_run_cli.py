import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import afk_run

ROOT = Path(__file__).parents[1]
PREFLIGHT_FIXTURE = ROOT / "tests" / "fixture_preflight_agent.py"


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
        self.preflight_scenario = "proceed"
        self.preflight_command = None
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
        preflight_input = json.loads((artifact / "preflight-input.json").read_text())
        preflight_output = json.loads(
            (artifact / "preflight" / "output.json").read_text()
        )
        self.assertEqual(bead["source"], {"kind": "bead", "id": "central-123"})
        self.assertEqual(assignment["source"], bead["source"])
        self.assertEqual(
            assignment["objective"],
            "Change the fixture\n\nDescription\nDo the requested work.\n\nDesign\nKeep it small.\n\nAcceptance criteria\nCommit the result.",
        )
        self.assertEqual(preparation["repository"]["base_commit"], self.base)
        self.assertEqual(preparation["project"]["slug"], "fixture")
        self.assertEqual(preparation["preparation_status"], "prepared")
        self.assertEqual(preparation["preflight"]["status"], "completed")
        self.assertEqual(preparation["preflight"]["decision"], "proceed")
        self.assertEqual(preflight_input["source"], bead["source"])
        self.assertEqual(
            preflight_input["acceptance_criteria"], bead["acceptance_criteria"]
        )
        self.assertEqual(preflight_output["decision"], "proceed")
        self.assertEqual(preparation["coordinator"]["exit_code"], 1)
        self.assertEqual(preparation["coordinator"]["outcome"], "failed")
        self.assertIsNone(preparation["coordinator"]["decision"])
        self.assertTrue(Path(assignment["workspace"]).is_dir())
        self.assertEqual(
            json.loads(
                (Path(assignment["workspace"]) / "worker-environment.json").read_text()
            ),
            {
                "unrelated": None,
                "pi_database": None,
                "openai_password": None,
                "anthropic_internal": None,
            },
        )
        self.assertEqual(request["assignment_path"], str(artifact / "assignment.json"))
        self.assertEqual(
            json.loads(
                (Path(assignment["workspace"]) / "worker-objective.json").read_text()
            ),
            {"objective": assignment["objective"]},
        )
        self.assertEqual(assignment["command"][-1], request["assignment_path"])
        self.assertTrue((artifact / "coordinator").is_dir())
        self.assertFalse((artifact / "publication.json").exists())
        self.assertNotIn(
            "TOP_SECRET",
            "".join(
                p.read_text(errors="replace")
                for p in artifact.rglob("*")
                if p.is_file()
            ),
        )

    def test_operator_evidence_pauses_before_coordinator_starts(self):
        self.preflight_scenario = "pause"
        self.bead["id"] = "central-6xx4.1"
        self.bead["title"] = "Register Operations WebUI as a first-class Project"
        self.bead["acceptance_criteria"] = (
            "Tests, build, deployment and HTTP verification pass."
        )
        self.write_bd()

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 1, result.stderr)
        artifact = self.artifact_from(result.stdout)
        preparation = json.loads((artifact / "preparation.json").read_text())
        preflight = json.loads((artifact / "preflight" / "output.json").read_text())
        self.assertEqual(preflight["decision"], "pause")
        self.assertEqual(preparation["preparation_status"], "paused")
        self.assertEqual(preparation["preflight"]["status"], "completed")
        self.assertEqual(preparation["preflight"]["decision"], "pause")
        self.assertEqual(preparation["coordinator"]["status"], "not_started")
        self.assertIsNone(preparation["coordinator"]["exit_code"])
        self.assertEqual(list((artifact / "coordinator").iterdir()), [])
        self.assertFalse(any(artifact.rglob("01-attempt")))
        self.assertIn("operator_external -> operator handoff", result.stdout)
        self.assertIn("preflight terminal decision", result.stdout)

    def test_retry_after_pause_creates_a_new_run_and_preserves_the_first(self):
        self.preflight_scenario = "pause"

        first = self.invoke("run", self.bead["id"], "--config", str(self.config))
        first_artifact = self.artifact_from(first.stdout)
        first_evidence = (first_artifact / "preparation.json").read_bytes()
        second = self.invoke("run", self.bead["id"], "--config", str(self.config))
        second_artifact = self.artifact_from(second.stdout)

        self.assertEqual((first.returncode, second.returncode), (1, 1))
        self.assertNotEqual(first_artifact, second_artifact)
        self.assertEqual(
            (first_artifact / "preparation.json").read_bytes(), first_evidence
        )
        self.assertEqual(
            json.loads((second_artifact / "preparation.json").read_text())[
                "preparation_status"
            ],
            "paused",
        )

    def test_interrupt_during_preflight_seals_terminal_state(self):
        marker = self.root / "preflight-child.pid"
        self.preflight_command = [
            sys.executable,
            str(PREFLIGHT_FIXTURE),
            "hang",
            str(marker),
        ]
        process = self.invoke_async(
            "run", self.bead["id"], "--config", str(self.config)
        )
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "preflight classifier did not start")

        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)

        self.assertEqual(process.returncode, 130, stderr)
        artifact = self.artifact_from(stdout)
        preparation = json.loads((artifact / "preparation.json").read_text())
        output = json.loads((artifact / "preflight" / "output.json").read_text())
        self.assertEqual(preparation["preparation_status"], "failed")
        self.assertEqual(preparation["preflight"]["status"], "failed")
        self.assertEqual(preparation["preflight"]["exit_code"], 130)
        self.assertEqual(preparation["preflight"]["outcome"], "interrupted")
        self.assertEqual(preparation["preflight"]["decision"], "pause")
        self.assertEqual(preparation["coordinator"]["status"], "not_started")
        self.assertEqual(output["outcome"], "interrupted")
        self.assertEqual(output["requests"], [])

    def test_missing_or_malformed_preflight_evidence_never_starts_coordinator(self):
        cases = ((None, "proceed"), ("Commit the result.", "invalid-classification"))
        for index, (acceptance, scenario) in enumerate(cases, 1):
            with self.subTest(scenario=scenario):
                if acceptance is None:
                    self.bead.pop("acceptance_criteria")
                else:
                    self.bead["acceptance_criteria"] = acceptance
                self.preflight_scenario = scenario
                self.bead["id"] = f"central-preflight-{index}"
                self.write_bd()

                result = self.invoke(
                    "run", self.bead["id"], "--config", str(self.config)
                )

                self.assertEqual(result.returncode, 1, result.stderr)
                artifact = self.artifact_from(result.stdout)
                preparation = json.loads((artifact / "preparation.json").read_text())
                self.assertEqual(preparation["preparation_status"], "failed")
                self.assertEqual(preparation["preflight"]["decision"], "pause")
                self.assertEqual(preparation["coordinator"]["status"], "not_started")
                self.assertEqual(list((artifact / "coordinator").iterdir()), [])

    def test_validation_evidence_is_required_before_run_creation(self):
        config = json.loads(self.config.read_text())
        config["projects"]["fixture"]["validation"].pop("evidence")
        self.config.write_text(json.dumps(config))

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 2)
        self.assertIn("validation is malformed", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_validation_evidence_must_be_bounded_before_run_creation(self):
        config = json.loads(self.config.read_text())
        config["projects"]["fixture"]["validation"]["evidence"] = "x" * 2001
        self.config.write_text(json.dumps(config))

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 2)
        self.assertIn("bounded nonempty text", result.stderr)
        self.assertFalse((self.root / "runs").exists())

    def test_configured_publication_exports_and_accepts_terminal_run(self):
        receipt = self.root / "publication-receipt.json"
        adapter = self.write_publication_adapter("accepted", 0, receipt)
        self.configure_publication(
            [sys.executable, str(adapter), "{bundle_path}", str(receipt)]
        )

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 1, result.stderr)
        artifact = self.artifact_from(result.stdout)
        publication = json.loads((artifact / "publication.json").read_text())
        self.assertEqual(publication["status"], "succeeded")
        self.assertEqual(publication["admission_outcome"], "accepted")
        self.assertEqual(publication["process"]["exit_code"], 0)
        self.assertIsNone(publication["error_category"])
        observed = json.loads(receipt.read_text())
        self.assertEqual(
            observed["identity"],
            {"project": "fixture", "run_id": artifact.name},
        )
        self.assertEqual(observed["bead"], "central-123")
        self.assertIn(
            "publication outcome for Bead central-123: accepted", result.stdout
        )
        self.assertFalse(Path(observed["bundle"]).exists())

    def test_same_terminal_run_replays_through_the_publication_seam(self):
        store = self.root / "adapter-store.json"
        adapter = self.write_stateful_publication_adapter(store)
        self.configure_publication(
            [sys.executable, str(adapter), "{bundle_path}", str(store)]
        )

        first = self.invoke("run", self.bead["id"], "--config", str(self.config))
        artifact = self.artifact_from(first.stdout)
        accepted = json.loads((artifact / "publication.json").read_text())
        config = afk_run.load_config(self.config)
        replayed = afk_run.publish_terminal_run(artifact, config["publication"])

        self.assertEqual(accepted["admission_outcome"], "accepted")
        self.assertEqual(replayed["status"], "succeeded")
        self.assertEqual(replayed["admission_outcome"], "replayed")

    def test_rejected_publication_preserves_a_completed_run_and_fails_the_cli(self):
        receipt = self.root / "rejected-receipt.json"
        adapter = self.write_publication_adapter("rejected", 1, receipt)
        self.configure_publication(
            [sys.executable, str(adapter), "{bundle_path}", str(receipt)]
        )
        terminal = self.completed_output("stop")

        code, _, artifact = self.run_with_coordinator_output(
            terminal, 0, complete_evidence=True
        )

        publication = json.loads((artifact / "publication.json").read_text())
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(publication["status"], "failed")
        self.assertEqual(publication["admission_outcome"], "rejected")
        self.assertEqual(publication["error_category"], "admission_rejected")
        self.assertEqual(preparation["coordinator"]["outcome"], "completed")
        self.assertEqual(preparation["coordinator"]["decision"], "stop")
        self.assertEqual(
            json.loads((artifact / "coordinator" / "output.json").read_text()),
            terminal,
        )

    def test_temporary_storage_failure_is_sealed_after_completed_run(self):
        receipt = self.root / "unused-receipt.json"
        adapter = self.write_publication_adapter("accepted", 0, receipt)
        self.configure_publication(
            [sys.executable, str(adapter), "{bundle_path}", str(receipt)]
        )
        with mock.patch(
            "afk_run.tempfile.TemporaryDirectory",
            side_effect=OSError("temporary storage unavailable"),
        ):
            code, _, artifact = self.run_with_coordinator_output(
                self.completed_output("stop"), 0, complete_evidence=True
            )

        publication = json.loads((artifact / "publication.json").read_text())
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(publication["error_category"], "temporary_storage")
        self.assertEqual(preparation["coordinator"]["decision"], "stop")

    def test_non_utf8_admission_output_is_a_protocol_failure(self):
        adapter = self.root / "non-utf8-adapter.py"
        adapter.write_text(
            "import os,sys\nos.write(1, b'\\xff\\xfe')\nraise SystemExit(0)\n"
        )
        self.configure_publication([sys.executable, str(adapter), "{bundle_path}"])

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        artifact = self.artifact_from(result.stdout)
        publication = json.loads((artifact / "publication.json").read_text())
        self.assertEqual(publication["error_category"], "admission_protocol")

    def test_malformed_publication_config_is_rejected_before_run_creation(self):
        config = json.loads(self.config.read_text())
        config["publication"] = {
            "command": ["publisher", "missing-placeholder"],
            "timeout_seconds": 5,
        }
        self.config.write_text(json.dumps(config))

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 2)
        self.assertIn("publication command", result.stderr)
        self.assertFalse((self.root / "runs").exists())

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

    def test_assignment_command_requires_one_exact_path_placeholder_before_mutation(
        self,
    ):
        for command in (
            [sys.executable, "-c", "pass"],
            ["worker", "prefix-{assignment_path}"],
            ["worker", "{assignment_path}", "{assignment_path}"],
        ):
            config = json.loads(self.config.read_text())
            config["assignment"]["command"] = command
            self.config.write_text(json.dumps(config))
            result = self.invoke("run", self.bead["id"], "--config", str(self.config))
            self.assertEqual(result.returncode, 2)
            self.assertIn("exactly one {assignment_path} argument", result.stderr)
            self.assertFalse((self.root / "runs").exists())

    def test_assignment_path_cannot_be_the_command_executable(self):
        config = json.loads(self.config.read_text())
        config["assignment"]["command"] = ["{assignment_path}"]
        self.config.write_text(json.dumps(config))

        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be the executable", result.stderr)
        self.assertFalse((self.root / "runs").exists())
        self.assertEqual(
            self.git("worktree", "list", "--porcelain").count("worktree "), 1
        )

    def test_flat_branch_avoids_prefix_conflict_and_namespace_collisions_are_preflighted(
        self,
    ):
        self.git("branch", f"afk/{self.bead['id']}")
        prefix = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertEqual(prefix.returncode, 1, prefix.stderr)
        preparation = json.loads(
            (self.artifact_from(prefix.stdout) / "preparation.json").read_text()
        )
        self.assertTrue(
            preparation["repository"]["branch"].startswith(f"afk-{self.bead['id']}-")
        )

        exact_run = "collision"
        exact_branch = f"afk-{self.bead['id']}-{exact_run}"
        self.git("branch", exact_branch)
        before = sorted((self.root / "runs" / self.bead["id"]).iterdir())
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("afk_run.new_run_id", return_value=exact_run),
        ):
            code = afk_run.run(self.bead["id"], self.config)
        self.assertEqual(code, 2)
        self.assertEqual(
            sorted((self.root / "runs" / self.bead["id"]).iterdir()), before
        )

        descendant_run = "descendant-collision"
        descendant_branch = f"afk-{self.bead['id']}-{descendant_run}"
        self.git("branch", f"{descendant_branch}/child")
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("afk_run.new_run_id", return_value=descendant_run),
        ):
            code = afk_run.run(self.bead["id"], self.config)
        self.assertEqual(code, 2)
        self.assertEqual(
            sorted((self.root / "runs" / self.bead["id"]).iterdir()), before
        )
        self.assertNotIn(
            f"worktree {self.root / 'worktrees' / self.bead['id'] / descendant_run}",
            self.git("worktree", "list", "--porcelain"),
        )

    def test_payload_write_failure_has_authoritative_sealed_evidence(self):
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch(
                "afk_run.write_json", side_effect=OSError("simulated payload failure")
            ),
        ):
            code = afk_run.run(self.bead["id"], self.config)
        self.assertEqual(code, 2)
        artifacts = list((self.root / "runs" / self.bead["id"]).iterdir())
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(preparation["preparation_status"], "failed")
        self.assertEqual(preparation["errors"][0]["category"], "filesystem")
        self.assertTrue((artifact / "coordinator").is_dir())

    def test_failed_worktree_creation_is_sealed_without_deleting_unknown_path(self):
        git = self.bin / "git"
        git.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n"
            "if sys.argv[1:3] == ['worktree', 'add']:\n"
            " path = Path(sys.argv[5]); path.mkdir(parents=True); "
            "(path / 'foreign').write_text('preserve me'); raise SystemExit(9)\n"
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
        failed_destination = Path(preparation["repository"]["worktree"])
        self.assertEqual((failed_destination / "foreign").read_text(), "preserve me")

    def test_repository_internal_and_symlinked_destinations_are_rejected(self):
        config = json.loads(self.config.read_text())
        config["run_root"] = str(self.repository / "runs")
        self.config.write_text(json.dumps(config))
        internal = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertNotEqual(internal.returncode, 0)
        self.assertIn("inside the selected repository", internal.stderr)
        self.assertFalse((self.repository / "runs").exists())

        self.write_config()
        run_root = self.root / "runs"
        run_root.mkdir()
        redirected = self.root / "redirected"
        redirected.mkdir()
        (run_root / self.bead["id"]).symlink_to(redirected, target_is_directory=True)
        symlinked = self.invoke("run", self.bead["id"], "--config", str(self.config))
        self.assertNotEqual(symlinked.returncode, 0)
        self.assertIn("artifact parent", symlinked.stderr)
        self.assertEqual(list(redirected.iterdir()), [])

    def test_computed_destination_inside_repository_is_rejected_before_mutation(self):
        run_root = self.root / "nested-runs"
        nested_repository = run_root / self.bead["id"]
        nested_repository.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=nested_repository, check=True)
        (nested_repository / "base").write_text("fixture\n")
        subprocess.run(["git", "add", "base"], cwd=nested_repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=AFK Test",
                "-c",
                "user.email=afk-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "base",
            ],
            cwd=nested_repository,
            check=True,
        )
        config = json.loads(self.config.read_text())
        config["run_root"] = str(run_root)
        config["projects"]["fixture"]["repository"] = str(nested_repository)
        self.config.write_text(json.dumps(config))

        before = sorted(path.name for path in nested_repository.iterdir())
        result = self.invoke("run", self.bead["id"], "--config", str(self.config))

        self.assertEqual(result.returncode, 2)
        self.assertIn("artifact destination", result.stderr)
        self.assertIn("inside the selected repository", result.stderr)
        self.assertEqual(
            sorted(path.name for path in nested_repository.iterdir()), before
        )

    def test_artifact_creation_race_does_not_overwrite_foreign_destination(self):
        run_id = "fixed-run"
        artifact = self.root / "runs" / self.bead["id"] / run_id
        original_mkdir = os.mkdir
        raced = False

        def mkdir_with_race(path, mode=0o777, *, dir_fd=None):
            nonlocal raced
            if path == run_id and dir_fd is not None and not raced:
                raced = True
                original_mkdir(path, mode, dir_fd=dir_fd)
                (artifact / "preparation.json").write_text("foreign evidence\n")
                (artifact / "foreign").write_text("preserve me\n")
            return original_mkdir(path, mode, dir_fd=dir_fd)

        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("afk_run.new_run_id", return_value=run_id),
            mock.patch("os.mkdir", new=mkdir_with_race),
        ):
            code = afk_run.run(self.bead["id"], self.config)

        self.assertEqual(code, 2)
        self.assertTrue(raced)
        self.assertEqual(
            (artifact / "preparation.json").read_text(), "foreign evidence\n"
        )
        self.assertEqual((artifact / "foreign").read_text(), "preserve me\n")
        self.assertFalse((artifact / "coordinator").exists())

    def test_parent_symlink_swap_cannot_redirect_owned_artifacts(self):
        run_id = "symlink-race"
        parent = self.root / "runs" / self.bead["id"]
        displaced = self.root / "runs" / "displaced-parent"
        redirected = self.root / "redirected"
        redirected.mkdir()
        original_mkdir = os.mkdir
        swapped = False

        def mkdir_with_swap(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == run_id and dir_fd is not None and not swapped:
                swapped = True
                parent.rename(displaced)
                parent.symlink_to(redirected, target_is_directory=True)
            return original_mkdir(path, mode, dir_fd=dir_fd)

        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("afk_run.new_run_id", return_value=run_id),
            mock.patch("os.mkdir", new=mkdir_with_swap),
        ):
            code = afk_run.run(self.bead["id"], self.config)

        self.assertEqual(code, 2)
        self.assertTrue(swapped)
        self.assertEqual(list(redirected.iterdir()), [])
        preparation = json.loads((displaced / run_id / "preparation.json").read_text())
        self.assertEqual(preparation["preparation_status"], "failed")
        self.assertIn("changed during preparation", preparation["errors"][0]["message"])

    def test_missing_bead_is_reported_without_leaking_reader_secret(self):
        self.write_bd(exit_code=1, stderr="TOP_SECRET database password")
        result = self.invoke("run", "central-missing", "--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("central-missing", result.stderr)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("TOP_SECRET", result.stderr + result.stdout)

    def test_terminal_decision_is_recorded_printed_and_controls_exit(self):
        for decision, expected_exit in (("stop", 0), ("exhausted", 1)):
            with self.subTest(decision=decision):
                code, stdout, artifact = self.run_with_coordinator_output(
                    self.completed_output(decision), 0
                )
                self.assertEqual(code, expected_exit)
                preparation = json.loads((artifact / "preparation.json").read_text())
                self.assertEqual(preparation["coordinator"]["status"], "completed")
                self.assertEqual(preparation["coordinator"]["outcome"], "completed")
                self.assertEqual(preparation["coordinator"]["decision"], decision)
                self.assertIn(
                    f"coordinator terminal decision for Bead central-123: {decision}",
                    stdout,
                )

    def test_malformed_terminal_output_is_value_safe_and_cannot_exit_zero(self):
        malformed = self.completed_output("continue")
        code, stdout, artifact = self.run_with_coordinator_output(malformed, 0)

        self.assertEqual(code, 1)
        preparation = json.loads((artifact / "preparation.json").read_text())
        self.assertEqual(preparation["coordinator"]["status"], "failed")
        self.assertEqual(preparation["coordinator"]["exit_code"], 0)
        self.assertIsNone(preparation["coordinator"]["outcome"])
        self.assertIsNone(preparation["coordinator"]["decision"])
        self.assertIn(
            "coordinator terminal decision for Bead central-123: unavailable", stdout
        )
        self.assertNotIn("continue", stdout)

    def completed_output(self, decision):
        components = (
            ("attempt", "succeeded", {"assignment": "assignment.json"}),
            (
                "validation",
                "passed",
                {"workspace": "assignment.json", "change": "01-attempt"},
            ),
            ("change", "completed", {"source": "01-attempt"}),
            (
                "review",
                "completed",
                {"change": "03-change", "validation": "02-validation"},
            ),
            ("assessment", "completed", {"review": "04-review"}),
            ("iteration", "completed", {"assessment": "05-assessment"}),
        )
        return {
            "schema_version": 1,
            "outcome": "completed",
            "decision": decision,
            "history": [
                {
                    "sequence": sequence,
                    "component": component,
                    "directory": f"{sequence:02d}-{component}",
                    "input_from": input_from,
                    "outcome": outcome,
                }
                for sequence, (component, outcome, input_from) in enumerate(
                    components, start=1
                )
            ],
        }

    def run_with_coordinator_output(self, output, exit_code, complete_evidence=False):
        original_run = subprocess.run

        def run(command, *args, **kwargs):
            if command[:3] == [sys.executable, "-m", "afk_coordinate"]:
                coordinator = Path(command[4])
                coordinator.mkdir()
                if complete_evidence:
                    self.write_completed_coordinator(coordinator, output)
                else:
                    (coordinator / "output.json").write_text(json.dumps(output))
                return subprocess.CompletedProcess(command, exit_code)
            return original_run(command, *args, **kwargs)

        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["AFK_PREFLIGHT_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(PREFLIGHT_FIXTURE), self.preflight_scenario]
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("afk_run.subprocess.run", side_effect=run),
            redirect_stdout(stdout),
        ):
            code = afk_run.run(self.bead["id"], self.config)
        artifact = self.artifact_from(stdout.getvalue())
        return code, stdout.getvalue(), artifact

    def write_completed_coordinator(self, coordinator, output):
        source = coordinator.parent
        assignment = json.loads((source / "assignment.json").read_text())
        request = json.loads((source / "coordinator-request.json").read_text())
        state = {
            "schema_version": 1,
            "status": "completed",
            "next_sequence": 7,
            "next_component": None,
            "active_invocation": None,
            "history": output["history"],
            "terminal": {"decision": output["decision"]},
        }
        for path, value in {
            coordinator / "assignment.json": assignment,
            coordinator / "input.json": request,
            coordinator / "state.json": state,
            coordinator / "output.json": output,
        }.items():
            path.write_text(json.dumps(value))
        outputs = {
            "01-attempt": {
                "schema_version": 1,
                "outcome": "succeeded",
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            },
            "02-validation": {
                "schema_version": 1,
                "outcome": "passed",
                "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
            },
            "03-change": {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "workspace": assignment["workspace"],
                    "objective": assignment["objective"],
                    "source": {
                        "kind": "attempt",
                        "directory": str(coordinator / "01-attempt"),
                    },
                    "repository": {},
                },
            },
            "04-review": {
                "schema_version": 1,
                "outcome": "completed",
                "review": {"summary": "Clean.", "findings": []},
                "artifacts": {
                    "diff": "review.diff",
                    "events": "events.jsonl",
                    "stderr": "stderr.log",
                },
            },
            "05-assessment": {
                "schema_version": 1,
                "outcome": "completed",
                "assessment": {"summary": "No findings.", "decisions": []},
                "artifacts": {"events": "events.jsonl", "stderr": "stderr.log"},
            },
            "06-iteration": {
                "schema_version": 1,
                "outcome": "completed",
                "policy": {
                    "decision": "stop",
                    "completed_responses": 0,
                    "max_responses": request["max_responses"],
                    "actionable_findings": 0,
                    "reason": "No actionable findings.",
                },
            },
        }
        for directory, value in outputs.items():
            result = coordinator / directory
            result.mkdir()
            (result / "output.json").write_text(json.dumps(value))
            for kind, artifact in value.get("artifacts", {}).items():
                content = ""
                if kind == "events":
                    content = '{"type":"agent_end"}\n'
                elif kind == "stdout":
                    content = "validation passed\n"
                elif kind == "diff":
                    content = "diff --git a/README.md b/README.md\n"
                (result / artifact).write_text(content)

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
                        "import json,os,sys; from pathlib import Path; "
                        "assignment=json.loads(Path(sys.argv[1]).read_text()); "
                        "Path('worker-objective.json').write_text(json.dumps("
                        "{'objective': assignment['objective']})); "
                        "Path('worker-environment.json').write_text(json.dumps("
                        "{'unrelated': os.getenv('UNRELATED_CANARY'), "
                        "'pi_database': os.getenv('PI_DATABASE_URL'), "
                        "'openai_password': os.getenv('OPENAI_INTERNAL_PASSWORD'), "
                        "'anthropic_internal': os.getenv('ANTHROPIC_INTERNAL_TOKEN')})); "
                        "raise SystemExit(7)"
                    ),
                    "{assignment_path}",
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
                        "evidence": "Repository tests and public behavior.",
                        "timeout_seconds": 5,
                    },
                }
            },
        }
        self.config.write_text(json.dumps(value))

    def configure_publication(self, command):
        value = json.loads(self.config.read_text())
        value["publication"] = {"command": command, "timeout_seconds": 5}
        self.config.write_text(json.dumps(value))

    def write_publication_adapter(self, outcome, exit_code, receipt):
        path = self.root / f"publish-{outcome}.py"
        path.write_text(
            "import json,sys\n"
            "from pathlib import Path\n"
            "bundle=Path(sys.argv[1])\n"
            "record=json.loads((bundle/'workflow-run.json').read_text())\n"
            "Path(sys.argv[2]).write_text(json.dumps({"
            "'identity': record['identity'], 'bead': record['bead']['id'], "
            "'bundle': str(bundle)}))\n"
            f"print(json.dumps({{'schema_version': 1, 'outcome': {outcome!r}, "
            "'identity': record['identity'], 'location': 'fixture/location'}))\n"
            f"raise SystemExit({exit_code})\n"
        )
        return path

    def write_stateful_publication_adapter(self, store):
        path = self.root / "stateful-publication.py"
        path.write_text(
            "import hashlib,json,sys\n"
            "from pathlib import Path\n"
            "bundle=Path(sys.argv[1])\n"
            "store=Path(sys.argv[2])\n"
            "manifest=json.loads((bundle/'manifest.json').read_text())\n"
            "digest=hashlib.sha256((bundle/'workflow-run.json').read_bytes()).hexdigest()\n"
            "outcome='replayed' if store.exists() and store.read_text()==digest else 'accepted'\n"
            "store.write_text(digest)\n"
            "print(json.dumps({'schema_version': 1, 'outcome': outcome, "
            "'identity': manifest['identity'], 'location': 'fixture/location'}))\n"
        )
        return path

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
        return subprocess.run(
            [str(ROOT / "afk"), *arguments],
            check=False,
            **self.invocation_options(),
        )

    def invoke_async(self, *arguments):
        return subprocess.Popen(
            [str(ROOT / "afk"), *arguments],
            **self.invocation_options(),
        )

    def invocation_options(self):
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        environment["UNRELATED_CANARY"] = "TOP_SECRET-must-not-be-forwarded"
        environment["PI_DATABASE_URL"] = "TOP_SECRET-pi-database"
        environment["OPENAI_INTERNAL_PASSWORD"] = "TOP_SECRET-openai-password"
        environment["ANTHROPIC_INTERNAL_TOKEN"] = "TOP_SECRET-anthropic-token"
        environment["AFK_PREFLIGHT_AGENT_COMMAND"] = json.dumps(
            self.preflight_command
            or [sys.executable, str(PREFLIGHT_FIXTURE), self.preflight_scenario]
        )
        return {
            "cwd": self.root,
            "env": environment,
            "text": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }

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
