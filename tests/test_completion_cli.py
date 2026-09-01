import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk_plan.contract import build_plan, validate_input
from afk_plan_accept.contract import accept_plan
from tests.test_plan_accept_contract import planner_input

ROOT = Path(__file__).parents[1]
FAKE_BD = ROOT / "tests" / "fixtures" / "fake_bd.py"


class CompletionRecordCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request, self.plan = external_plan()
        self.acceptance, self.publication, self.child_id = self.publish_plan(
            self.request, self.plan, "external"
        )
        self.completion_input = self.root / "completion.json"
        self.result = self.root / "completion-result"
        self.write_input(self.human_record())

    def publish_plan(self, request, plan, prefix):
        acceptance_directory = self.root / f"{prefix}-acceptance"
        acceptance_directory.mkdir()
        publication_directory = self.root / f"{prefix}-publication"
        beads = self.root / f"{prefix}-beads"
        beads.mkdir()
        state = beads / "state.json"
        accepted = accept_plan(request, plan)
        (acceptance_directory / "input.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "planner_input": request,
                    "plan": plan,
                }
            )
        )
        (acceptance_directory / "output.json").write_text(
            json.dumps(acceptance_output(request, accepted))
        )
        state.write_text(
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
        publish_input = self.root / f"{prefix}-publish.json"
        publish_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_directory": str(acceptance_directory),
                    "beads_workspace": str(beads),
                    "command": [sys.executable, str(FAKE_BD), str(state)],
                    "timeout_seconds": 30,
                }
            )
        )
        published = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_plan_publish",
                publish_input,
                publication_directory,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        child_id = json.loads((publication_directory / "output.json").read_text())[
            "children"
        ][1]["bead_id"]
        return acceptance_directory, publication_directory, child_id

    def test_validates_one_manual_external_check_end_to_end(self):
        completed = self.invoke()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "satisfied")
        self.assertEqual(output["evidence_basis"], "external_check")
        self.assertEqual(output["source"], {"kind": "bead", "id": self.child_id})
        self.assertEqual(output["record"], self.human_record())

    def test_rejects_retired_approval_producer_kinds(self):
        for kind in ("human_attestation", "human_waiver"):
            with self.subTest(kind=kind):
                record = self.human_record()
                record["producer"]["kind"] = kind
                self.write_input(record)
                result = self.root / f"result-{kind}"

                completed = self.invoke(result=result)

                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertFalse(result.exists())

    def test_stale_plan_or_subject_fails_before_result_creation(self):
        for name, mutate in (
            (
                "plan",
                lambda value: value["record"].__setitem__("parent_plan", "0" * 64),
            ),
            (
                "subject",
                lambda value: value["record"]["subject"].__setitem__(
                    "commit", "different"
                ),
            ),
        ):
            with self.subTest(name=name):
                value = self.input_value(self.human_record())
                mutate(value)
                path = self.root / f"{name}.json"
                result = self.root / f"{name}-result"
                path.write_text(json.dumps(value))
                completed = self.invoke(path, result)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())

    def test_wrong_authority_criteria_or_producer_fails_closed(self):
        mutations = (
            lambda record: record["producer"].__setitem__("identity", "Someone else"),
            lambda record: record.__setitem__("criteria", ["criterion-1"]),
            lambda record: record["producer"].__setitem__("kind", "repository_check"),
        )
        for index, mutate in enumerate(mutations):
            record = self.human_record()
            mutate(record)
            path = self.root / f"invalid-{index}.json"
            result = self.root / f"invalid-{index}-result"
            path.write_text(json.dumps(self.input_value(record)))
            completed = self.invoke(path, result)
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(result.exists())

    def test_tampered_publication_mapping_fails_closed(self):
        output_path = self.publication / "output.json"
        output = json.loads(output_path.read_text())
        output["children"][1]["local_id"] = "invented"
        output_path.write_text(json.dumps(output))

        completed = self.invoke()

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

    def human_record(self):
        return {
            "schema_version": 1,
            "child": self.child_id,
            "parent_plan": self.plan["plan_sha256"],
            "outcome": "satisfied",
            "producer": {"kind": "external_check", "identity": "Deployment verifier"},
            "criteria": ["criterion-2"],
            "subject": {"commit": "abc123", "environment": "local production"},
            "evidence": ["bead-comment:central-example#approval-1"],
            "accepted_at": "2026-08-22T23:00:00Z",
        }

    def input_value(self, record):
        return {
            "schema_version": 1,
            "acceptance_directory": str(self.acceptance),
            "publication_directory": str(self.publication),
            "expected_subject": {
                "commit": "abc123",
                "environment": "local production",
            },
            "record": record,
        }

    def write_input(self, record):
        self.completion_input.write_text(json.dumps(self.input_value(record)))

    def invoke(self, path=None, result=None):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_complete",
                path or self.completion_input,
                result or self.result,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


class PublicCompletionRecordCliTest(unittest.TestCase):
    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_complete", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        malformed = subprocess.run(
            [sys.executable, "-m", "afk_complete"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(help_result.returncode, 0)
        self.assertIn("COMPLETION_JSON RESULT_DIRECTORY", help_result.stdout)
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("usage:", malformed.stderr)


def acceptance_output(request, acceptance):
    return {
        "schema_version": 2,
        "outcome": "completed",
        "decision": "accepted",
        "source": {"kind": "bead", "id": request["parent"]["id"]},
        "started_at": "2026-08-22T00:00:00Z",
        "finished_at": "2026-08-22T00:00:01Z",
        "duration_seconds": 1,
        "policy": "contract-valid-capability-plan-v2",
        "acceptance": acceptance,
        "error_category": None,
        "artifacts": {"input": "input.json"},
    }


def external_plan():
    request = planner_input()
    owner = "Deployment verifier"
    executor = "outside_help"
    evidence_route = "external_check"
    request["catalog"]["projects"][0]["routes"].append(
        {
            "owner": owner,
            "executor": executor,
            "outside_help_reason": "unavailable_system",
            "evidence_route": evidence_route,
            "phases": ["closure"],
        }
    )
    request = validate_input(request)
    plan = build_plan(
        request,
        {
            "schema_version": 2,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": "The change is implemented.",
                    "statement": "Implement the change.",
                },
                {
                    "id": "criterion-2",
                    "source_text": "The current documentation is updated.",
                    "statement": "Approve the documentation.",
                },
            ],
            "children": [
                {
                    "local_id": "implementation",
                    "title": "Implement the change",
                    "objective": "Implement the requested behavior.",
                    "criteria": ["criterion-1"],
                    "project": "example",
                    "owner": "AFK Run",
                    "phase": "implementation",
                    "executor": "afk_run",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                },
                {
                    "local_id": "verification",
                    "title": "Verify the documentation",
                    "objective": "Verify the published documentation.",
                    "criteria": ["criterion-2"],
                    "project": "example",
                    "owner": owner,
                    "phase": "closure",
                    "executor": executor,
                    "outside_help_reason": "unavailable_system",
                    "evidence_route": evidence_route,
                    "depends_on": ["implementation"],
                },
            ],
            "ambiguities": [],
        },
    )
    return request, plan


if __name__ == "__main__":
    unittest.main()
