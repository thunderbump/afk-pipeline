import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
ATTEMPT_FIXTURE = ROOT / "tests" / "fixture_agent.py"
REVIEW_FIXTURE = ROOT / "tests" / "fixture_review_agent.py"
ASSESSMENT_FIXTURE = ROOT / "tests" / "fixture_assessment_agent.py"
RESPONSE_FIXTURE = ROOT / "tests" / "fixture_response_agent.py"


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

        usage = (
            "usage: python3 -m afk_coordinate RUN_JSON RUN_DIRECTORY [--abandon-active]"
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(usage, help_result.stdout)
        self.assertIn("resume", help_result.stdout)
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

    def test_restart_consumes_a_result_sealed_after_the_coordinator_crashed(self):
        assignment_path, request_path = self.prepare_run(max_responses=0)
        run = self.root / "resumed-run"
        environment = self.environment(review_scenario="delayed-no-findings")
        coordinator = subprocess.Popen(
            self.command(request_path, run),
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.wait_for_active(run, "review")
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
        self.assertEqual(review["agent"]["status"], "error")
        output = json.loads((run / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["failed_component"], "review")
        self.assertEqual(output["component_outcome"], "failed")
        self.assertEqual(
            [item["outcome"] for item in output["history"]],
            ["succeeded", "passed", "completed", "failed"],
        )

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
        environment["AFK_REVIEW_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(REVIEW_FIXTURE), review_scenario]
        )
        environment["AFK_ASSESS_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(ASSESSMENT_FIXTURE), assessment_scenario]
        )
        environment["AFK_RESPOND_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(RESPONSE_FIXTURE), response_scenario]
        )
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state_path = run / "state.json"
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
