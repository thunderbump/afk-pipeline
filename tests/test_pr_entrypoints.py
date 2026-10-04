"""Current command imports and publication do not load standalone stages."""

import importlib.abc
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).parents[1]
OLD_MODULES = {"afk_run", "afk_export", "afk_coordinate", "afk_plan"}


class RejectOldImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".", 1)[0] in OLD_MODULES:
            raise AssertionError(f"current command imported old module {fullname}")


class EntrypointTests(unittest.TestCase):
    def invoke(self, *arguments):
        # A fresh process prevents old imports elsewhere in test discovery
        # from hiding a reverse dependency through sys.modules.
        script = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from tests.test_pr_entrypoints import RejectOldImports; "
            "sys.meta_path.insert(0, RejectOldImports()); "
            "import runpy; "
            f"sys.argv = [{str(ROOT / 'afk')!r}, *sys.argv[1:]]; "
            f"runpy.run_path({str(ROOT / 'afk')!r}, run_name='__main__')"
        )
        with tempfile.TemporaryDirectory() as outside:
            return subprocess.run(
                [sys.executable, "-c", script, *arguments],
                cwd=outside,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )

    def test_public_help_and_current_subcommands_load_outside_checkout(self):
        help_result = self.invoke("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for name in (
            "pr",
            "evaluate",
            "review",
            "respond",
            "status",
            "context",
            "finish",
            "assess",
            "job",
            "cleanup",
            "gc",
            "orchestrate",
        ):
            with self.subTest(name=name):
                self.assertIn(name, help_result.stdout)
                result = self.invoke(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("afk run", help_result.stdout)
        self.assertNotIn("afk export", help_result.stdout)

    def test_missing_old_and_unknown_commands_fail_parser_without_execution(self):
        for arguments in (
            (),
            ("run", "central-example"),
            ("continue", "old-run", "1"),
            ("export", "old-run", "bundle"),
            ("coordinate",),
            ("plan",),
            ("validate",),
            ("unknown",),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_internal_pr_and_orchestrator_worker_routes_remain_callable(self):
        from afk_orchestrate import __main__ as orchestrate
        from afk_pr.__main__ import main

        with mock.patch("afk_pr.__main__.worker") as worker:
            self.assertEqual(main(["worker", "/private/job", "fixtures"]), 0)
            worker.assert_called_once_with(Path("/private/job"), "fixtures")
        with mock.patch.object(orchestrate, "worker") as worker:
            self.assertEqual(main(["orchestrate", "worker", "/private/state"]), 0)
            worker.assert_called_once_with(Path("/private/state"))

    def test_current_publication_paths_run_with_old_imports_forbidden(self):
        cases = [
            "tests.test_pr_creation.CreationTests.test_initial_implementation_pushes_one_commit_creates_draft_and_queues_fixtures",
            "tests.test_pr_creation.CreationTests.test_private_notes_reach_attempt_without_entering_public_pr_template",
            "tests.test_pr_passes.JobTests.test_failure_diagnostics_are_bounded_redacted_and_published",
            "tests.test_pr_passes.JobTests.test_public_log_redaction_sees_headers_before_the_displayed_tail",
            "tests.test_pr_response.ResponseTests.test_response_publication_redacts_credentials_and_withholds_private_keys",
            "tests.test_pr_diagnostics.DiagnosticTests.test_invalid_or_missing_summary_is_optional_and_never_published",
        ]
        script = (
            "import sys, unittest; "
            "from tests.test_pr_entrypoints import RejectOldImports; "
            "sys.meta_path.insert(0, RejectOldImports()); "
            f"suite = unittest.defaultTestLoader.loadTestsFromNames({cases!r}); "
            "result = unittest.TextTestRunner(verbosity=2).run(suite); "
            "raise SystemExit(not result.wasSuccessful())"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BeadHelperTests(unittest.TestCase):
    def record(self):
        return {
            "id": "central-task",
            "title": "  Task title  ",
            "description": "  Do the work.  ",
            "design": None,
            "acceptance_criteria": "  Result accepted.  ",
            "labels": ["project:fixture"],
            "notes": "private tracker note",
        }

    def test_reader_keeps_subprocess_credentials_private_and_validates_records(self):
        from afk_pr import beads

        record = self.record()
        environment = {"BEADS_DOLT_PASSWORD": "test-only-credential"}
        with mock.patch.object(beads.subprocess, "run") as read:
            for value in (record, [record]):
                read.return_value = SimpleNamespace(
                    returncode=0, stdout=json.dumps(value)
                )
                self.assertEqual(
                    beads.read_bead("central-task", Path("/central"), env=environment),
                    record,
                )
                read.assert_called_with(
                    ["bd", "show", "central-task", "--json"],
                    cwd=Path("/central"),
                    env=environment,
                    timeout=120,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            for value in (
                [],
                [record, record],
                {**record, "id": "other"},
                {**record, "title": " "},
                {**record, "labels": [1]},
                {**record, "description": {}},
            ):
                with self.subTest(value=value):
                    read.return_value = SimpleNamespace(
                        returncode=0, stdout=json.dumps(value)
                    )
                    with self.assertRaises(beads.PreparationError):
                        beads.read_bead(
                            "central-task", Path("/central"), env=environment
                        )
        self.assertNotEqual(
            os.environ.get("BEADS_DOLT_PASSWORD"), "test-only-credential"
        )

    def test_ownership_and_public_snapshot_do_not_include_private_notes(self):
        from afk_pr import beads

        record = self.record()
        self.assertEqual(beads.ownership(record["id"], record["labels"]), "fixture")
        for labels in (
            [],
            ["project:a", "project:b"],
            ["project:../unsafe"],
            ["project:a", "project:a"],
        ):
            with self.subTest(labels=labels), self.assertRaises(beads.PreparationError):
                beads.ownership(record["id"], labels)
        snapshot = beads.safe_bead(record["id"], record)
        self.assertNotIn("notes", snapshot)
        self.assertNotIn("design", snapshot)
        self.assertEqual(snapshot["source"], {"kind": "bead", "id": record["id"]})
        self.assertEqual(
            beads.objective(snapshot),
            "Task title\n\nDescription\nDo the work.\n\nAcceptance criteria\nResult accepted.",
        )


if __name__ == "__main__":
    unittest.main()
