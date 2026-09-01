import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from afk_plan_accept.contract import accept_plan
from tests.test_plan_accept_contract import (
    capability_input,
    capability_plan,
    planner_input,
    proposed_plan,
)

ROOT = Path(__file__).parents[1]
FAKE_BD = ROOT / "tests" / "fixtures" / "fake_bd.py"


class ChildGraphPublisherCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.beads = self.root / "beads"
        self.beads.mkdir()
        self.state = self.beads / "state.json"
        request = planner_input()
        plan = proposed_plan(request)
        acceptance = accept_plan(request, plan)
        self.acceptance = self.root / "acceptance"
        self.acceptance.mkdir()
        self.replace_acceptance(request, plan, acceptance)
        self.state.write_text(
            json.dumps(
                {
                    "parent": {
                        **request["parent"],
                        "status": "in_progress",
                        "issue_type": "task",
                        "priority": 2,
                        "dependencies": [],
                    },
                    "children": [],
                }
            )
        )
        self.publisher_input = self.root / "publisher.json"
        self.result = self.root / "result"
        self.publisher_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_directory": str(self.acceptance),
                    "beads_workspace": str(self.beads),
                    "command": [sys.executable, str(FAKE_BD), str(self.state)],
                    "timeout_seconds": 30,
                }
            )
        )

    def test_publishes_children_and_dependencies_from_one_accepted_plan(self):
        completed = self.invoke(self.result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "published")
        self.assertEqual(
            output["children"],
            [
                {"local_id": "implementation", "bead_id": "central-child-1"},
                {"local_id": "documentation", "bead_id": "central-child-2"},
            ],
        )
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["children"]), 2)
        self.assertEqual(
            state["children"][0]["labels"],
            ["project:example", "ready-for-agent"],
        )
        self.assertIn(
            {"id": "central-child-1", "dependency_type": "blocks"},
            state["children"][1]["dependencies"],
        )

    def test_replays_the_same_plan_without_duplicate_children(self):
        first = self.invoke(self.result)
        replay_result = self.root / "replay"

        replay = self.invoke(replay_result)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        output = json.loads((replay_result / "output.json").read_text())
        self.assertEqual(output["decision"], "replayed")
        self.assertEqual(len(json.loads(self.state.read_text())["children"]), 2)

    def test_retry_reconciles_a_failed_partial_publication(self):
        state = json.loads(self.state.read_text())
        state["fail_create_attempt"] = 2
        self.state.write_text(json.dumps(state))

        failed = self.invoke(self.result)

        self.assertEqual(failed.returncode, 1, failed.stderr)
        failed_output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(failed_output["outcome"], "failed")
        self.assertEqual(
            failed_output["children"],
            [{"local_id": "implementation", "bead_id": "central-child-1"}],
        )
        retry_result = self.root / "retry"
        retried = self.invoke(retry_result)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(len(json.loads(self.state.read_text())["children"]), 2)

    def test_interrupt_seals_the_known_partial_mapping(self):
        state = json.loads(self.state.read_text())
        state["sleep_create_attempt"] = 2
        self.state.write_text(json.dumps(state))
        process = subprocess.Popen(
            self.command(self.result),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.state.read_text())
            if state.get("create_attempts") == 2:
                break
            time.sleep(0.02)
        else:
            process.kill()
            self.fail("publisher did not reach the injected interruption point")

        os.killpg(process.pid, signal.SIGINT)
        _, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 130, stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["decision"], "interrupted")
        self.assertEqual(output["error_category"], "interrupted")
        self.assertEqual(
            output["children"],
            [{"local_id": "implementation", "bead_id": "central-child-1"}],
        )

    def test_v2_publishes_caller_agent_and_outside_help_distinctly(self):
        request = capability_input()
        plan = capability_plan(request, executor="outside_help")
        self.replace_acceptance(request, plan)
        state = json.loads(self.state.read_text())
        state["parent"] = {
            **request["parent"],
            "status": "in_progress",
            "issue_type": "task",
            "priority": 2,
            "dependencies": [],
        }
        self.state.write_text(json.dumps(state))

        completed = self.invoke(self.result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        children = json.loads(self.state.read_text())["children"]
        self.assertEqual(children[0]["labels"], ["project:example", "ready-for-agent"])
        self.assertNotIn("handoff", children[0]["description"].lower())
        self.assertEqual(children[1]["labels"], ["project:example", "ready-for-human"])
        self.assertIn("## Outside capability required", children[1]["description"])
        self.assertIn("`missing_credentials`", children[1]["description"])
        self.assertNotIn("approval", children[1]["description"].lower())

    def test_tampered_acceptance_causes_no_beads_command_or_result(self):
        output_path = self.acceptance / "output.json"
        output = json.loads(output_path.read_text())
        output["acceptance"]["acceptance_sha256"] = "0" * 64
        output_path.write_text(json.dumps(output))
        before = self.state.read_bytes()

        completed = self.invoke(self.result)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.result.exists())

    def test_incomplete_policy_envelope_causes_no_beads_command_or_result(self):
        output_path = self.acceptance / "output.json"
        output = json.loads(output_path.read_text())
        del output["policy"]
        output_path.write_text(json.dumps(output))
        before = self.state.read_bytes()

        completed = self.invoke(self.result)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.result.exists())

    def test_replay_rejects_an_extra_plan_controlled_dependency(self):
        first = self.invoke(self.result)
        self.assertEqual(first.returncode, 0, first.stderr)
        state = json.loads(self.state.read_text())
        state["children"][0]["dependencies"].append(
            {"id": "central-unplanned", "dependency_type": "blocks"}
        )
        self.state.write_text(json.dumps(state))

        replay_result = self.root / "replay-extra-edge"
        replay = self.invoke(replay_result)

        self.assertEqual(replay.returncode, 1)
        self.assertEqual(
            json.loads((replay_result / "output.json").read_text())["outcome"],
            "failed",
        )

    def replace_acceptance(self, request, plan, acceptance=None):
        acceptance = acceptance or accept_plan(request, plan)
        (self.acceptance / "input.json").write_text(
            json.dumps(
                {
                    "schema_version": request["schema_version"],
                    "planner_input": request,
                    "plan": plan,
                }
            )
        )
        (self.acceptance / "output.json").write_text(
            json.dumps(
                {
                    "schema_version": request["schema_version"],
                    "outcome": "completed",
                    "decision": "accepted",
                    "source": {"kind": "bead", "id": request["parent"]["id"]},
                    "started_at": "2026-08-22T00:00:00Z",
                    "finished_at": "2026-08-22T00:00:01Z",
                    "duration_seconds": 1,
                    "policy": acceptance["policy"],
                    "acceptance": acceptance,
                    "error_category": None,
                    "artifacts": {"input": "input.json"},
                }
            )
        )

    def invoke(self, result):
        return subprocess.run(
            self.command(result),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def command(self, result):
        return [
            sys.executable,
            "-m",
            "afk_plan_publish",
            self.publisher_input,
            result,
        ]


if __name__ == "__main__":
    unittest.main()
