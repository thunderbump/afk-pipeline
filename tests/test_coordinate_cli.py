import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from afk_related_work import build_snapshot, reference

ROOT = Path(__file__).parents[1]
ATTEMPT_FIXTURE = ROOT / "tests" / "fixture_agent.py"
INFERENCE_FIXTURE = ROOT / "tests" / "inference_coordinate_fixture.py"


class PublicCoordinatorCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_coordinate", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        malformed = subprocess.run(
            [sys.executable, "-m", "afk_coordinate", "run.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        usage = "usage: python3 -m afk_coordinate RUN_JSON RUN_DIRECTORY"
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(usage, help_result.stdout)
        self.assertIn("resume", help_result.stdout)
        self.assertIn("ADDITIONAL_RESPONSES", help_result.stdout)
        self.assertEqual(help_result.stderr, "")
        self.assertEqual(malformed.returncode, 2)
        self.assertIn(usage, malformed.stderr)


class CoordinatorCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("fixture repository\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Initial state")

    def test_no_finding_run_completes_through_existing_components(self):
        assignment = {
            "schema_version": 1,
            "objective": "Implement the fixture change.",
            "workspace": str(self.workspace),
            "command": [sys.executable, str(ATTEMPT_FIXTURE), "git-commit"],
            "timeout_seconds": 5,
        }
        assignment_path = self.root / "assignment.json"
        self.write_json(assignment_path, assignment)
        request = {
            "schema_version": 1,
            "assignment_path": str(assignment_path),
            "validation": {
                "command": [sys.executable, "-c", "pass"],
                "timeout_seconds": 5,
            },
            "agent_timeout_seconds": 5,
            "max_responses": 1,
        }
        request_path = self.root / "run.json"
        self.write_json(request_path, request)
        run = self.root / "run"

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads((run / "input.json").read_text()), request)
        self.assertEqual(json.loads((run / "assignment.json").read_text()), assignment)
        expected_history = [
            {
                "sequence": 1,
                "component": "attempt",
                "directory": "01-attempt",
                "input_from": {"assignment": "assignment.json"},
                "outcome": "succeeded",
            },
            {
                "sequence": 2,
                "component": "validation",
                "directory": "02-validation",
                "input_from": {
                    "workspace": "assignment.json",
                    "change": "01-attempt",
                },
                "outcome": "passed",
            },
            {
                "sequence": 3,
                "component": "change",
                "directory": "03-change",
                "input_from": {"source": "01-attempt"},
                "outcome": "completed",
            },
            {
                "sequence": 4,
                "component": "review",
                "directory": "04-review",
                "input_from": {
                    "change": "03-change",
                    "validation": "02-validation",
                },
                "outcome": "completed",
            },
            {
                "sequence": 5,
                "component": "assessment",
                "directory": "05-assessment",
                "input_from": {"review": "04-review"},
                "outcome": "completed",
            },
            {
                "sequence": 6,
                "component": "iteration",
                "directory": "06-iteration",
                "input_from": {"assessment": "05-assessment"},
                "outcome": "completed",
            },
        ]
        self.assertEqual(
            json.loads((run / "state.json").read_text()),
            {
                "schema_version": 1,
                "status": "completed",
                "next_sequence": 7,
                "next_component": None,
                "active_invocation": None,
                "history": expected_history,
                "terminal": {"decision": "stop"},
            },
        )
        self.assertEqual(
            json.loads((run / "output.json").read_text()),
            {
                "schema_version": 1,
                "outcome": "completed",
                "decision": "stop",
                "history": expected_history,
            },
        )
        for directory in [item["directory"] for item in expected_history]:
            self.assertTrue((run / directory / "output.json").is_file())

    def test_actionable_run_responds_and_exhausts_at_the_caller_limit(self):
        assignment_path, request_path = self.prepare_run(max_responses=1)
        run = self.root / "bounded-run"

        completed = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
            response_scenario="commit",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "exhausted")
        self.assertEqual(
            [item["component"] for item in output["history"]],
            [
                "attempt",
                "validation",
                "change",
                "review",
                "assessment",
                "iteration",
                "response",
                "validation",
                "change",
                "review",
                "assessment",
                "iteration",
            ],
        )
        self.assertEqual(
            json.loads((run / "07-response" / "output.json").read_text())["outcome"],
            "completed",
        )
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertTrue(assignment_path.is_file())

    def test_exhausted_run_adds_responses_without_repeating_attempt(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "continued-run"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        original_state = (run / "state.json").read_bytes()
        original_output = (run / "output.json").read_bytes()

        continued = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="commit",
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(continued.returncode, 0, continued.stderr)
        continuation = run / "continuations" / "01"
        self.assertEqual(
            json.loads((continuation / "input.json").read_text()),
            {
                "schema_version": 1,
                "additional_responses": 1,
                "completed_responses": 0,
                "effective_max_responses": 1,
                "prior_output": "../../output.json",
            },
        )
        output = json.loads((continuation / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(output["history"][6]["component"], "response")
        self.assertEqual(
            [item["component"] for item in output["history"]].count("attempt"), 1
        )
        self.assertEqual((run / "state.json").read_bytes(), original_state)
        self.assertEqual((run / "output.json").read_bytes(), original_output)
        self.assertEqual(
            json.loads((run / "12-iteration" / "input.json").read_text())[
                "max_responses"
            ],
            1,
        )

    def test_continuation_reuses_and_enforces_frozen_related_work(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        records = {
            "task": {"id": "task", "title": "Current", "parent": "epic"},
            "epic": {
                "id": "epic",
                "title": "Parent",
                "children": ["task", "callers"],
            },
            "callers": {"id": "callers", "title": "Migrate callers"},
        }
        raw, facts = build_snapshot(records["task"], records.__getitem__)
        snapshot = self.root / "related-work.jsonl"
        snapshot.write_bytes(raw)
        related = reference(snapshot, facts)
        assignment = json.loads(assignment_path.read_text())
        assignment.update(
            {
                "related_work": related,
                "related_work_instructions": "Assignment is authoritative.",
            }
        )
        self.write_json(assignment_path, assignment)
        request = json.loads(request_path.read_text())
        request["related_work"] = related
        self.write_json(request_path, request)
        run = self.root / "related-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)

        changed_request = dict(request)
        changed_request["related_work"] = {**related, "sha256": "0" * 64}
        self.write_json(request_path, changed_request)
        changed_reference = self.invoke(request_path, run, "--continue-exhausted", "1")
        self.assertEqual(changed_reference.returncode, 2)
        self.assertIn("reference disagrees", changed_reference.stderr)
        self.assertFalse((run / "continuations").exists())

        self.write_json(request_path, request)
        continued = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="commit",
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        output = json.loads((run / "continuations" / "01" / "output.json").read_text())
        review_directory = next(
            item["directory"]
            for item in reversed(output["history"])
            if item["component"] == "review"
        )
        review_input = json.loads((run / review_directory / "input.json").read_text())
        self.assertEqual(review_input["related_work"], related)

        snapshot.write_bytes(raw.replace(b"Migrate callers", b"Rewrite callers"))
        changed_snapshot = self.invoke(request_path, run, "--continue-exhausted", "1")
        self.assertEqual(changed_snapshot.returncode, 2)
        self.assertIn("digest disagrees", changed_snapshot.stderr)
        self.assertFalse((run / "continuations" / "02").exists())

    def test_export_selects_newest_terminal_and_preserves_continuation_lineage(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "published-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        original_state = (run / "state.json").read_bytes()
        original_output = (run / "output.json").read_bytes()
        stopped = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="commit",
        )
        bundle = self.root / "continuation-bundle"

        exported = subprocess.run(
            [
                str(ROOT / "afk"),
                "export",
                str(run),
                str(bundle),
                "--project",
                "fixture",
                "--run-id",
                "continued-1",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        record = json.loads((bundle / "workflow-run.json").read_text())
        self.assertEqual(record["identity"]["run_id"], "continued-1.continuation.01")
        self.assertEqual(record["terminal"], {"decision": "stop"})
        sources = {item["source"]["path"] for item in record["artifacts"]}
        self.assertIn("state.json", sources)
        self.assertIn("continuations/01/output.json", sources)
        self.assertEqual((run / "state.json").read_bytes(), original_state)
        self.assertEqual((run / "output.json").read_bytes(), original_output)

        continuation_input = run / "continuations" / "01" / "input.json"
        malformed = json.loads(continuation_input.read_text())
        malformed["prior_output"] = "../../wrong.json"
        continuation_input.write_text(json.dumps(malformed))
        rejected = subprocess.run(
            [
                str(ROOT / "afk"),
                "export",
                str(run),
                str(self.root / "malformed-bundle"),
                "--project",
                "fixture",
                "--run-id",
                "continued-1",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertFalse((self.root / "malformed-bundle").exists())

    def test_exhausted_continuation_resumes_after_coordinator_interruption(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "interrupted-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        environment = self.environment(
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="delayed-commit",
        )
        coordinator = subprocess.Popen(
            self.command(request_path, run, "--continue-exhausted", "1"),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        continuation = run / "continuations" / "01"
        self.wait_for_active_state(continuation / "state.json", "response")
        os.kill(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=5)
        self.wait_for_file(run / "07-response" / "output.json")

        resumed = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads((continuation / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            [record["component"] for record in output["history"]].count("response"),
            1,
        )
        self.assertFalse((run / "08-response").exists())

    def test_orphaned_continuation_invocation_can_be_abandoned_and_retried(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "orphaned-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        environment = self.environment(
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="delayed-commit",
        )
        coordinator = subprocess.Popen(
            self.command(request_path, run, "--continue-exhausted", "1"),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        continuation = run / "continuations" / "01"
        self.wait_for_active_state(continuation / "state.json", "response")
        os.killpg(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=5)

        resumed = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            "--abandon-active",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="commit",
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads((continuation / "output.json").read_text())
        responses = [
            record for record in output["history"] if record["component"] == "response"
        ]
        self.assertEqual(
            [record["outcome"] for record in responses], ["abandoned", "completed"]
        )
        self.assertEqual(output["decision"], "stop")

    def test_abandon_without_an_active_continuation_refuses_before_allocation(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "no-active-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )

        abandoned = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            "--abandon-active",
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(abandoned.returncode, 2)
        self.assertIn("there is no active invocation to abandon", abandoned.stderr)
        self.assertFalse((run / "continuations").exists())

    def test_active_continuation_refuses_a_rewritten_response_allowance(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "tampered-continuation"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        environment = self.environment(
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="delayed-commit",
        )
        coordinator = subprocess.Popen(
            self.command(request_path, run, "--continue-exhausted", "1"),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        continuation = run / "continuations" / "01"
        state_path = continuation / "state.json"
        self.wait_for_active_state(state_path, "response")
        os.kill(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=5)
        self.wait_for_file(run / "07-response" / "output.json")
        continuation_input = json.loads((continuation / "input.json").read_text())
        continuation_input["additional_responses"] = 2
        continuation_input["effective_max_responses"] = 2
        self.write_json(continuation / "input.json", continuation_input)
        before = state_path.read_bytes()

        resumed = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "2",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
        )

        self.assertEqual(resumed.returncode, 2)
        self.assertIn("continuation lineage", resumed.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_each_exhausted_continuation_adds_a_fresh_response_allowance(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "multiple-continuations"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        first = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="findings",
            assessment_scenario="address",
            response_scenario="commit",
        )
        first_output = (run / "continuations" / "01" / "output.json").read_bytes()

        second = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "2",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
            response_scenario="commit",
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_directory = run / "continuations" / "02"
        self.assertEqual(
            json.loads((second_directory / "input.json").read_text()),
            {
                "schema_version": 1,
                "additional_responses": 2,
                "completed_responses": 1,
                "effective_max_responses": 3,
                "prior_output": "../01/output.json",
            },
        )
        output = json.loads((second_directory / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            [record["component"] for record in output["history"]].count("response"),
            2,
        )
        self.assertEqual(
            (run / "continuations" / "01" / "output.json").read_bytes(),
            first_output,
        )

        # Publication can select an immutable predecessor while still validating
        # the complete retained lineage.
        from afk_export import ExportError, export_run

        bundle = self.root / "first-continuation-bundle"
        exported = export_run(
            run,
            bundle,
            project="fixture",
            run_id="continued-1",
            terminal_continuation="01",
        )
        record = json.loads((bundle / "workflow-run.json").read_text())
        self.assertEqual(exported["identity"]["run_id"], "continued-1.continuation.01")
        self.assertEqual(record["terminal"], {"decision": "exhausted"})
        sources = {artifact["source"]["path"] for artifact in record["artifacts"]}
        self.assertIn("continuations/01/output.json", sources)
        self.assertNotIn("continuations/02/output.json", sources)

        second_input = json.loads((second_directory / "input.json").read_text())
        second_input["prior_output"] = "../wrong/output.json"
        (second_directory / "input.json").write_text(json.dumps(second_input))
        with self.assertRaises((ExportError, ValueError)):
            export_run(
                run,
                self.root / "invalid-predecessor-bundle",
                project="fixture",
                run_id="continued-1",
                terminal_continuation="01",
            )

    def test_next_continuation_refuses_rewritten_predecessor_allowance(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "rewritten-predecessor"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        first = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="findings",
            assessment_scenario="address",
            response_scenario="commit",
        )
        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(first.returncode, 0, first.stderr)
        continuation = run / "continuations" / "01"
        continuation_input = json.loads((continuation / "input.json").read_text())
        continuation_input["additional_responses"] = 2
        continuation_input["effective_max_responses"] = 2
        self.write_json(continuation / "input.json", continuation_input)
        continuation_state = json.loads((continuation / "state.json").read_text())
        continuation_state["continuation"] = continuation_input
        self.write_json(continuation / "state.json", continuation_state)

        second = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            review_scenario="no-findings",
            assessment_scenario="no-findings",
        )

        self.assertEqual(second.returncode, 2)
        self.assertIn("matching Iteration evidence", second.stderr)
        self.assertFalse((run / "continuations" / "02").exists())

    def test_stopped_run_refuses_continuation_without_changing_terminal_evidence(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "stopped-run"
        stopped = self.invoke(request_path, run)
        state = (run / "state.json").read_bytes()
        output = (run / "output.json").read_bytes()

        continued = self.invoke(request_path, run, "--continue-exhausted", "1")

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(continued.returncode, 2)
        self.assertIn("only an exhausted", continued.stderr)
        self.assertEqual((run / "state.json").read_bytes(), state)
        self.assertEqual((run / "output.json").read_bytes(), output)
        self.assertFalse((run / "continuations").exists())

    def test_failed_run_refuses_continuation_without_changing_terminal_evidence(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "failed-continuation-origin"
        failed = self.invoke(request_path, run)
        state = (run / "state.json").read_bytes()
        output = (run / "output.json").read_bytes()

        continued = self.invoke(request_path, run, "--continue-exhausted", "1")

        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(continued.returncode, 2)
        self.assertIn("only an exhausted", continued.stderr)
        self.assertEqual((run / "state.json").read_bytes(), state)
        self.assertEqual((run / "output.json").read_bytes(), output)
        self.assertFalse((run / "continuations").exists())

    def test_advanced_workspace_refuses_continuation_before_allocating_a_ledger(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "advanced-workspace"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
        )
        original_state = (run / "state.json").read_bytes()
        original_output = (run / "output.json").read_bytes()
        (self.workspace / "README.md").write_text("externally advanced\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "External repair")

        continued = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(continued.returncode, 2)
        self.assertIn(
            "workspace must match the assessed repository state", continued.stderr
        )
        self.assertEqual((run / "state.json").read_bytes(), original_state)
        self.assertEqual((run / "output.json").read_bytes(), original_output)
        self.assertFalse((run / "continuations").exists())

    def test_continuation_requires_a_positive_additional_response_count(self):
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value):
                result = self.invoke(
                    self.root / "missing-request.json",
                    self.root / "missing-run",
                    "--continue-exhausted",
                    value,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "ADDITIONAL_RESPONSES must be a positive integer", result.stderr
                )
                self.assertFalse((self.root / "missing-run").exists())

    def test_continuation_requires_an_existing_run(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "missing-existing-run"

        continued = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
        )

        self.assertEqual(continued.returncode, 2)
        self.assertIn("existing Coordinator Run", continued.stderr)
        self.assertFalse(run.exists())

    def test_restart_consumes_a_result_sealed_after_the_coordinator_crashed(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "resumed-run"
        environment = self.environment(review_scenario="delayed-no-findings")
        review_started = self.root / "delayed-review-started"
        environment["AFK_TEST_REVIEW_STARTED_MARKER"] = str(review_started)
        coordinator = subprocess.Popen(
            self.command(request_path, run),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.wait_for_active(run, "review")
        # The active checkpoint is durable before the component process starts.
        # Synchronize with the delayed fixture so killing the coordinator tests
        # consumption of a result sealed by an already-running component rather
        # than racing process launch under a loaded validation run.
        self.wait_for_file(review_started)
        os.kill(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=5)
        self.wait_for_file(run / "04-review" / "output.json")
        assignment_path.unlink()

        resumed = self.invoke(request_path, run)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            [item["component"] for item in output["history"]],
            ["attempt", "validation", "change", "review", "assessment", "iteration"],
        )
        self.assertFalse((run / "active-input.json").exists())

    def test_unsealed_active_invocation_requires_explicit_abandonment(self):
        marker = self.root / "attempt-started.json"
        assignment = {
            "schema_version": 1,
            "objective": "Implement the fixture change.",
            "workspace": str(self.workspace),
            "command": [
                sys.executable,
                str(ATTEMPT_FIXTURE),
                "hang-once",
                str(marker),
            ],
            "timeout_seconds": 30,
        }
        assignment_path = self.root / "retry-assignment.json"
        self.write_json(assignment_path, assignment)
        request_path = self.root / "retry-run.json"
        self.write_json(
            request_path,
            {
                "schema_version": 1,
                "assignment_path": str(assignment_path),
                "validation": {
                    "command": [sys.executable, "-c", "pass"],
                    "timeout_seconds": 5,
                },
                "agent_timeout_seconds": 5,
                "max_responses": 0,
            },
        )
        run = self.root / "retry-run"
        coordinator = subprocess.Popen(
            self.command(request_path, run),
            cwd=ROOT,
            env=self.environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.wait_for_file(marker)
        processes = json.loads(marker.read_text())
        os.kill(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=5)
        for process in (processes["wrapper"], processes["agent"]):
            try:
                os.kill(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
        before = (run / "state.json").read_text()

        unresolved = self.invoke(request_path, run)

        self.assertEqual(unresolved.returncode, 1, unresolved.stderr)
        self.assertEqual((run / "state.json").read_text(), before)
        self.assertFalse((run / "output.json").exists())

        resumed = self.invoke(request_path, run, "--abandon-active")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            output["history"][:2],
            [
                {
                    "sequence": 1,
                    "component": "attempt",
                    "directory": "01-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "abandoned",
                },
                {
                    "sequence": 2,
                    "component": "attempt",
                    "directory": "02-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "succeeded",
                },
            ],
        )

    def test_resume_rejects_a_checkpoint_directory_that_escapes_the_run(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        assignment = json.loads(assignment_path.read_text())
        run = self.root / "forged-run"
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        state = {
            "schema_version": 1,
            "status": "running",
            "next_sequence": 2,
            "next_component": "attempt",
            "active_invocation": {
                "sequence": 1,
                "component": "attempt",
                "directory": "../forged-result",
                "input_from": {"assignment": "assignment.json"},
            },
            "history": [],
            "terminal": None,
        }
        self.write_json(run / "state.json", state)
        forged = self.root / "forged-result"
        forged.mkdir()
        self.write_json(forged / "output.json", {"outcome": "succeeded"})
        before = (run / "state.json").read_text()

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid coordinator checkpoint", completed.stderr)
        self.assertEqual((run / "state.json").read_text(), before)
        self.assertFalse((run / "output.json").exists())

    def test_invalid_input_and_abandon_without_a_run_create_no_evidence(self):
        malformed_path = self.root / "malformed.json"
        self.write_json(malformed_path, {"schema_version": 1})
        malformed_run = self.root / "malformed-run"

        malformed = self.invoke(malformed_path, malformed_run)

        self.assertEqual(malformed.returncode, 2)
        self.assertFalse(malformed_run.exists())

        _assignment_path, request_path = self.prepare_run(max_responses=0)
        absent_run = self.root / "absent-run"

        abandoned = self.invoke(request_path, absent_run, "--abandon-active")

        self.assertEqual(abandoned.returncode, 2)
        self.assertIn("no active invocation", abandoned.stderr)
        self.assertFalse(absent_run.exists())

    def test_malformed_review_events_fail_the_run_deterministically(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "malformed-review-run"

        completed = self.invoke(request_path, run, review_scenario="null-content")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        review = json.loads((run / "04-review" / "output.json").read_text())
        self.assertEqual(review["outcome"], "failed")
        self.assertIsNone(review["agent"])
        receipt = json.loads(
            (run / "04-review" / "inference" / "receipt.json").read_text()
        )
        self.assertEqual(receipt["protocol"]["status"], "protocol_malformed")
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["failed_component"], "review")
        self.assertEqual(output["component_outcome"], "failed")
        self.assertEqual(
            [item["outcome"] for item in output["history"]],
            ["succeeded", "passed", "completed", "failed"],
        )

    def test_ordinary_validation_failure_is_repaired_within_response_allowance(self):
        _assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(0 if 'response applied' in Path('README.md').read_text() else 7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "validation-repair-run"

        completed = self.invoke(
            request_path, run, response_scenario="validation-repair"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            [record["component"] for record in output["history"]],
            [
                "attempt",
                "validation",
                "response",
                "validation",
                "change",
                "review",
                "assessment",
                "iteration",
            ],
        )
        self.assertEqual(output["history"][1]["outcome"], "failed")
        response_input = json.loads((run / "03-response" / "input.json").read_text())
        self.assertEqual(
            response_input["validation_directory"],
            str((run / "02-validation").resolve()),
        )
        prompt_events = (run / "03-response" / "events.jsonl").read_text()
        self.assertIn("Repaired repository validation", prompt_events)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_resumed_validation_repair_rechecks_repository_drift(self):
        assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "drifted-validation-repair"
        self.prepare_validation_repair_checkpoint(assignment_path, request_path, run)
        (self.workspace / "README.md").write_text("drifted after validation\n")

        resumed = self.invoke(request_path, run, response_scenario="validation-repair")

        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["failed_component"], "validation")
        self.assertEqual(
            [record["component"] for record in output["history"]],
            ["attempt", "validation"],
        )
        self.assertIsNone(
            json.loads((run / "state.json").read_text())["active_invocation"]
        )
        self.assertFalse((run / "03-response").exists())

    def test_resumed_validation_repair_rechecks_exhausted_allowance(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "exhausted-resumed-validation-repair"
        self.prepare_validation_repair_checkpoint(assignment_path, request_path, run)

        resumed = self.invoke(request_path, run, response_scenario="validation-repair")

        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["failed_component"], "validation")
        self.assertEqual(
            [record["component"] for record in output["history"]],
            ["attempt", "validation"],
        )
        self.assertFalse((run / "03-response").exists())

    def test_abandoned_validation_repair_can_be_retried(self):
        assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(0 if 'response applied' in Path('README.md').read_text() else 7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "abandoned-validation-repair"
        self.prepare_validation_repair_checkpoint(
            assignment_path, request_path, run, active_response=True
        )

        resumed = self.invoke(
            request_path,
            run,
            "--abandon-active",
            response_scenario="validation-repair",
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads((run / "output.json").read_text())
        responses = [
            record for record in output["history"] if record["component"] == "response"
        ]
        self.assertEqual(
            [record["outcome"] for record in responses], ["abandoned", "completed"]
        )
        self.assertEqual(responses[1]["input_from"], {"validation": "02-validation"})
        response_input = json.loads((run / "04-response" / "input.json").read_text())
        self.assertEqual(
            response_input["validation_directory"],
            str((run / "02-validation").resolve()),
        )

    def test_continuation_and_export_preserve_validation_repair_history(self):
        _assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(0 if 'response applied' in Path('README.md').read_text() else 7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "continued-validation-repair"
        exhausted = self.invoke(
            request_path,
            run,
            review_scenario="findings",
            assessment_scenario="address",
            response_scenario="validation-repair",
        )

        continued = self.invoke(
            request_path,
            run,
            "--continue-exhausted",
            "1",
            response_scenario="commit",
        )
        bundle = self.root / "validation-repair-bundle"
        exported = subprocess.run(
            [
                str(ROOT / "afk"),
                "export",
                str(run),
                str(bundle),
                "--project",
                "fixture",
                "--run-id",
                "validation-repair",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(exhausted.returncode, 0, exhausted.stderr)
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        output = json.loads((run / "continuations" / "01" / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(
            [record["component"] for record in output["history"]].count("response"),
            2,
        )
        self.assertEqual(output["history"][1]["outcome"], "failed")
        self.assertTrue((bundle / "workflow-run.json").is_file())

    def test_validation_launch_error_cannot_allocate_repair(self):
        _assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [str(self.root / "missing-validation")]
        self.write_json(request_path, request)
        run = self.root / "validation-launch-error"

        completed = self.invoke(
            request_path, run, response_scenario="validation-repair"
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(
            [record["component"] for record in output["history"]],
            ["attempt", "validation"],
        )
        validation = json.loads((run / "02-validation" / "output.json").read_text())
        self.assertIn("error", validation["process"])
        self.assertFalse((run / "03-response").exists())

    def test_repeated_validation_failure_exhausts_allowance_without_extra_repair(self):
        _assignment_path, request_path = self.prepare_run(max_responses=1)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.write_json(request_path, request)
        run = self.root / "validation-repair-exhausted"

        completed = self.invoke(
            request_path, run, response_scenario="validation-repair"
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["failed_component"], "validation")
        self.assertEqual(
            [record["component"] for record in output["history"]],
            ["attempt", "validation", "response", "validation"],
        )
        self.assertEqual(
            [record["component"] for record in output["history"]].count("response"),
            1,
        )
        self.assertFalse((run / "05-response").exists())

    def test_sealed_component_failure_seals_the_run_and_resume_is_idempotent(self):
        _assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        request["validation"]["command"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        self.write_json(request_path, request)
        run = self.root / "failed-run"

        failed = self.invoke(request_path, run)

        self.assertEqual(failed.returncode, 1, failed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(
            {
                key: output[key]
                for key in ("outcome", "failed_component", "component_outcome")
            },
            {
                "outcome": "failed",
                "failed_component": "validation",
                "component_outcome": "failed",
            },
        )
        self.assertEqual(
            [item["outcome"] for item in output["history"]],
            ["succeeded", "failed"],
        )
        before = (run / "state.json").read_text()

        resumed = self.invoke(request_path, run)

        self.assertEqual(resumed.returncode, 1, resumed.stderr)
        self.assertEqual((run / "state.json").read_text(), before)

    def test_failed_checkpoint_without_terminal_output_is_finalized_on_resume(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        assignment = json.loads(assignment_path.read_text())
        run = self.root / "failed-checkpoint"
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        history = [
            {
                "sequence": 1,
                "component": "attempt",
                "directory": "01-attempt",
                "input_from": {"assignment": "assignment.json"},
                "outcome": "failed",
            }
        ]
        state = {
            "schema_version": 1,
            "status": "failed",
            "next_sequence": 2,
            "next_component": None,
            "active_invocation": None,
            "history": history,
            "terminal": {
                "failed_component": "attempt",
                "component_outcome": "failed",
                "exit_code": 1,
            },
        }
        self.write_json(run / "state.json", state)

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            json.loads((run / "output.json").read_text()),
            {
                "schema_version": 1,
                "outcome": "failed",
                **state["terminal"],
                "history": history,
            },
        )

    def test_resume_between_modules_starts_the_recorded_next_component(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        assignment = json.loads(assignment_path.read_text())
        run = self.root / "between-modules"
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        attempt = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_attempt",
                str(run / "assignment.json"),
                str(run / "01-attempt"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(attempt.returncode, 0, attempt.stderr)
        self.write_json(
            run / "state.json",
            {
                "schema_version": 1,
                "status": "running",
                "next_sequence": 2,
                "next_component": "validation",
                "active_invocation": None,
                "history": [
                    {
                        "sequence": 1,
                        "component": "attempt",
                        "directory": "01-attempt",
                        "input_from": {"assignment": "assignment.json"},
                        "outcome": "succeeded",
                    }
                ],
                "terminal": None,
            },
        )

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["decision"], "stop")
        self.assertEqual(output["history"][1]["component"], "validation")
        self.assertFalse((run / "02-attempt").exists())

    def test_malformed_iteration_decision_is_refused_without_changing_state(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        assignment = json.loads(assignment_path.read_text())
        run = self.root / "malformed-iteration"
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        history = []
        components = ["attempt", "validation", "change", "review", "assessment"]
        sources = [
            {"assignment": "assignment.json"},
            {"workspace": "assignment.json", "change": "01-attempt"},
            {"source": "01-attempt"},
            {"change": "03-change", "validation": "02-validation"},
            {"review": "04-review"},
        ]
        outcomes = ["succeeded", "passed", "completed", "completed", "completed"]
        for sequence, (component, input_from, outcome) in enumerate(
            zip(components, sources, outcomes, strict=True), start=1
        ):
            history.append(
                {
                    "sequence": sequence,
                    "component": component,
                    "directory": f"{sequence:02d}-{component}",
                    "input_from": input_from,
                    "outcome": outcome,
                }
            )
        state = {
            "schema_version": 1,
            "status": "running",
            "next_sequence": 7,
            "next_component": "iteration",
            "active_invocation": {
                "sequence": 6,
                "component": "iteration",
                "directory": "06-iteration",
                "input_from": {"assessment": "05-assessment"},
            },
            "history": history,
            "terminal": None,
        }
        self.write_json(run / "state.json", state)
        (run / "06-iteration").mkdir()
        self.write_json(
            run / "06-iteration" / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "policy": {"decision": "anything"},
            },
        )
        before = (run / "state.json").read_text()

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid iteration output", completed.stderr)
        self.assertEqual((run / "state.json").read_text(), before)
        self.assertFalse((run / "output.json").exists())

    def test_resume_rejects_an_impossible_checkpoint_transition(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        request = json.loads(request_path.read_text())
        assignment = json.loads(assignment_path.read_text())
        run = self.root / "impossible-transition"
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        state = {
            "schema_version": 1,
            "status": "running",
            "next_sequence": 2,
            "next_component": "response",
            "active_invocation": None,
            "history": [
                {
                    "sequence": 1,
                    "component": "attempt",
                    "directory": "01-attempt",
                    "input_from": {"assignment": "assignment.json"},
                    "outcome": "succeeded",
                }
            ],
            "terminal": None,
        }
        self.write_json(run / "state.json", state)
        before = (run / "state.json").read_text()

        completed = self.invoke(request_path, run)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid coordinator checkpoint", completed.stderr)
        self.assertEqual((run / "state.json").read_text(), before)
        self.assertFalse((run / "output.json").exists())

    def prepare_run(self, max_responses):
        assignment = {
            "schema_version": 1,
            "objective": "Implement the fixture change.",
            "workspace": str(self.workspace),
            "command": [sys.executable, str(ATTEMPT_FIXTURE), "git-commit"],
            "timeout_seconds": 5,
        }
        assignment_path = self.root / f"assignment-{max_responses}.json"
        self.write_json(assignment_path, assignment)
        request = {
            "schema_version": 1,
            "assignment_path": str(assignment_path),
            "validation": {
                "command": [sys.executable, "-c", "pass"],
                "timeout_seconds": 5,
            },
            "agent_timeout_seconds": 5,
            "max_responses": max_responses,
        }
        request_path = self.root / f"run-{max_responses}.json"
        self.write_json(request_path, request)
        return assignment_path, request_path

    def prepare_validation_repair_checkpoint(
        self, assignment_path, request_path, run, active_response=False
    ):
        assignment = json.loads(assignment_path.read_text())
        request = json.loads(request_path.read_text())
        run.mkdir()
        self.write_json(run / "input.json", request)
        self.write_json(run / "assignment.json", assignment)
        attempt = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_attempt",
                str(run / "assignment.json"),
                str(run / "01-attempt"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(attempt.returncode, 0, attempt.stderr)
        validation_input = {
            "schema_version": 1,
            "workspace": assignment["workspace"],
            **request["validation"],
        }
        self.write_json(run / "active-input.json", validation_input)
        validation = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_validate",
                str(run / "active-input.json"),
                str(run / "02-validation"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 1, validation.stderr)
        (run / "active-input.json").unlink()
        history = [
            {
                "sequence": 1,
                "component": "attempt",
                "directory": "01-attempt",
                "input_from": {"assignment": "assignment.json"},
                "outcome": "succeeded",
            },
            {
                "sequence": 2,
                "component": "validation",
                "directory": "02-validation",
                "input_from": {
                    "workspace": "assignment.json",
                    "change": "01-attempt",
                },
                "outcome": "failed",
            },
        ]
        active = None
        if active_response:
            active = {
                "sequence": 3,
                "component": "response",
                "directory": "03-response",
                "input_from": {"validation": "02-validation"},
            }
        self.write_json(
            run / "state.json",
            {
                "schema_version": 1,
                "status": "running",
                "next_sequence": 4 if active_response else 3,
                "next_component": "response",
                "active_invocation": active,
                "history": history,
                "terminal": None,
            },
        )

    def invoke(
        self,
        request_path,
        run,
        *extra,
        review_scenario="no-findings",
        assessment_scenario="no-findings",
        response_scenario="commit",
    ):
        environment = self.environment(
            review_scenario, assessment_scenario, response_scenario
        )
        return subprocess.run(
            self.command(request_path, run, *extra),
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def environment(
        self,
        review_scenario="no-findings",
        assessment_scenario="no-findings",
        response_scenario="commit",
    ):
        environment = os.environ.copy()
        bin_directory = self.root / "inference-bin"
        bin_directory.mkdir(exist_ok=True)
        command = " ".join(
            shlex.quote(item)
            for item in (
                sys.executable,
                str(INFERENCE_FIXTURE),
                review_scenario,
                assessment_scenario,
                response_scenario,
            )
        )
        pi = bin_directory / "pi"
        pi.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
        pi.chmod(0o755)
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        return environment

    def command(self, request_path, run, *extra):
        return [
            sys.executable,
            "-m",
            "afk_coordinate",
            str(request_path),
            str(run),
            *extra,
        ]

    def wait_for_active(self, run, component):
        self.wait_for_active_state(run / "state.json", component)

    def wait_for_active_state(self, state_path, component):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if state_path.exists():
                state = json.loads(state_path.read_text())
                active = state["active_invocation"]
                if active is not None and active["component"] == component:
                    return
            time.sleep(0.01)
        self.fail(f"coordinator did not start {component}")

    def wait_for_file(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"file was not created: {path}")

    def git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.workspace,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def write_json(self, path, value):
        path.write_text(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
