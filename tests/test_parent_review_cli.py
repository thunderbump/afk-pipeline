import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk_plan.contract import build_plan, validate_input
from afk_plan_accept.contract import accept_plan
from tests.test_completion_cli import acceptance_output
from tests.test_plan_accept_contract import planner_input

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_parent_review_agent.py"
COMMIT = "a" * 40


class ParentAcceptanceReviewCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request, self.plan = mixed_plan()
        self.acceptance = self.root / "acceptance"
        self.publication = self.root / "publication"
        self.completions = self.root / "completions"
        self.completions.mkdir()
        self.pipeline = self.root / "pipeline-run"
        self.write_evidence()
        self.input_path = self.root / "review.json"
        self.result = self.root / "result"

    def test_accepts_complete_mixed_owner_fan_in(self):
        completed = self.invoke("accepted")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "accepted")
        self.assertEqual(
            [item["id"] for item in output["review"]["criteria"]],
            ["criterion-1", "criterion-2"],
        )
        self.assertEqual(output["review"]["gaps"], [])
        self.assertIsNone(output["review"]["follow_up"])
        self.assertEqual(output["reviewer"]["model"], "gpt-5.6-luna")
        self.assertEqual(output["artifacts"]["input"], "input.json")

    def test_accepts_published_v2_capability_fan_in(self):
        self.request, self.plan = v2_repository_plan()
        accepted = accept_plan(self.request, self.plan)
        (self.acceptance / "input.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "planner_input": self.request,
                    "plan": self.plan,
                }
            )
        )
        accepted_output = acceptance_output(self.request, accepted)
        accepted_output["schema_version"] = 2
        accepted_output["policy"] = "contract-valid-capability-plan-v2"
        (self.acceptance / "output.json").write_text(json.dumps(accepted_output))
        (self.publication / "output.json").write_text(
            json.dumps(publication_output(self.request, accepted))
        )
        check_directories = []
        for index, child in enumerate(self.plan["children"], start=1):
            child_id = f"central-child-{index}"
            evidence_kind = child["evidence_route"]
            record = {
                "schema_version": 1,
                "child": child_id,
                "parent_plan": self.plan["plan_sha256"],
                "outcome": "satisfied",
                "producer": {"kind": evidence_kind, "identity": child["owner"]},
                "criteria": child["criteria"],
                "subject": {"commit": COMMIT},
                "evidence": [f"repository-check:{child['local_id']}"],
                "accepted_at": "2026-08-23T00:00:02Z",
            }
            completion = self.completions / child["local_id"]
            completion.mkdir(exist_ok=True)
            (completion / "input.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "acceptance_directory": str(self.acceptance),
                        "publication_directory": str(self.publication),
                        "expected_subject": {"commit": COMMIT},
                        "record": record,
                    }
                )
            )
            (completion / "output.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "completed",
                        "decision": "satisfied",
                        "source": {"kind": "bead", "id": child_id},
                        "started_at": "2026-08-23T00:00:00Z",
                        "finished_at": "2026-08-23T00:00:01Z",
                        "duration_seconds": 1,
                        "acceptance_sha256": accepted["acceptance_sha256"],
                        "plan_sha256": self.plan["plan_sha256"],
                        "local_id": child["local_id"],
                        "criteria": child["criteria"],
                        "evidence_basis": evidence_kind,
                        "satisfies_criteria": True,
                        "record": record,
                        "error_category": None,
                        "artifacts": {"input": "input.json"},
                    }
                )
            )
            check = self.root / f"repository-check-{index}"
            check.mkdir()
            state = {"head": COMMIT, "dirty": False, "status": []}
            (check / "input.json").write_text(json.dumps({"schema_version": 1}))
            (check / "output.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "passed",
                        "finished_at": "2026-08-23T00:00:01Z",
                        "process": {"exit_code": 0, "signal": None, "error": None},
                        "repository": {
                            "head_changed": False,
                            "before": state,
                            "after": state,
                        },
                    }
                )
            )
            check_directories.append(check)
        request = self.input_value()
        request["completions"][1]["local_id"] = "outside-check"
        request["completions"][1]["directory"] = str(self.completions / "outside-check")
        for completion, check, child in zip(
            request["completions"],
            check_directories,
            self.plan["children"],
            strict=True,
        ):
            completion["terminal"] = (
                {
                    "kind": "repository_check",
                    "directory": str(check),
                }
                if child["evidence_route"] == "repository_check"
                else {"kind": "completion_record"}
            )

        completed = self.invoke("accepted", request)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        fan_in = json.loads((self.result / "fan-in.json").read_text())
        self.assertEqual(
            [child["execution"] for child in fan_in["children"]],
            ["caller_agent", "outside_help"],
        )
        self.assertEqual(
            [child["terminal"]["status"] for child in fan_in["children"]],
            ["passed", "validated"],
        )

    def test_seals_incomplete_with_explicit_gap_and_follow_up(self):
        completed = self.invoke("incomplete")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["decision"], "incomplete")
        self.assertEqual(output["review"]["gaps"][0]["criterion"], "criterion-1")
        self.assertEqual(output["review"]["follow_up"]["criteria"], ["criterion-1"])
        self.assertEqual(output["review"]["follow_up"]["project"], "example")
        self.assertEqual(
            output["review"]["follow_up"]["evidence_route"], "pipeline_run"
        )

    def test_rejects_nonterminal_or_topologically_wrong_child_graph(self):
        for name, mutate in (
            (
                "open",
                lambda value: value["child_graph"][0].__setitem__(
                    "status", "in_progress"
                ),
            ),
            (
                "edge",
                lambda value: value["child_graph"][1].__setitem__("dependencies", []),
            ),
        ):
            with self.subTest(name=name):
                request = self.input_value()
                mutate(request)
                result = self.root / f"result-{name}"
                completed = self.invoke("accepted", request, result)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())

    def test_rejects_tampered_or_missing_completion_coverage(self):
        request = self.input_value()
        request["completions"].pop()
        missing = self.invoke("accepted", request, self.root / "missing")
        self.assertEqual(missing.returncode, 2)

        output_path = self.completions / "implementation" / "output.json"
        output = json.loads(output_path.read_text())
        output["plan_sha256"] = "0" * 64
        output_path.write_text(json.dumps(output))
        tampered = self.invoke("accepted", result=self.root / "tampered")
        self.assertEqual(tampered.returncode, 2)

    def test_rejects_stale_subject_and_unsuccessful_terminal_evidence(self):
        request = self.input_value()
        request["completions"][0]["current_subject"]["commit"] = "b" * 40
        stale = self.invoke("accepted", request, self.root / "stale")
        self.assertEqual(stale.returncode, 2)

        coordinator_output = self.pipeline / "coordinator" / "output.json"
        output = json.loads(coordinator_output.read_text())
        output["decision"] = "exhausted"
        coordinator_output.write_text(json.dumps(output))
        unsuccessful = self.invoke("accepted", result=self.root / "unsuccessful")
        self.assertEqual(unsuccessful.returncode, 2)

    def test_non_satisfying_waiver_cannot_be_accepted_by_inference(self):
        directory = self.completions / "approval"
        input_path = directory / "input.json"
        output_path = directory / "output.json"
        input_value = json.loads(input_path.read_text())
        output = json.loads(output_path.read_text())
        for value in (input_value["record"], output["record"]):
            value["producer"]["kind"] = "human_waiver"
            value["outcome"] = "waived"
        output["decision"] = "waived"
        output["evidence_basis"] = "human_waiver"
        output["satisfies_criteria"] = False
        input_path.write_text(json.dumps(input_value))
        output_path.write_text(json.dumps(output))

        completed = self.invoke("accepted")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        sealed = json.loads((self.result / "output.json").read_text())
        self.assertEqual(sealed["outcome"], "failed")
        self.assertEqual(sealed["error_category"], "invalid_review")

    def test_agent_protocol_and_process_failures_are_sealed(self):
        for scenario, category in (
            ("invalid-events", "agent_protocol"),
            ("process-failure", "agent_process"),
        ):
            with self.subTest(scenario=scenario):
                result = self.root / f"result-{scenario}"
                completed = self.invoke(scenario, result=result)
                self.assertEqual(completed.returncode, 1)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertEqual(output["error_category"], category)
                self.assertIsNone(output["review"])

    def test_help_and_malformed_invocation_use_conventional_exits(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "afk_parent_review", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        malformed = subprocess.run(
            [sys.executable, "-m", "afk_parent_review"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("REVIEW_JSON RESULT_DIRECTORY", help_result.stdout)
        self.assertEqual(malformed.returncode, 2)

    def test_invalid_agent_configuration_does_not_create_an_attempt(self):
        self.input_path.write_text(json.dumps(self.input_value()))
        environment = os.environ.copy()
        environment["AFK_PARENT_REVIEW_AGENT_COMMAND"] = "not JSON"

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_parent_review",
                self.input_path,
                self.result,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.result.exists())

    def invoke(self, scenario, request=None, result=None):
        self.input_path.write_text(json.dumps(request or self.input_value()))
        environment = os.environ.copy()
        environment["AFK_PARENT_REVIEW_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(FIXTURE), scenario]
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_parent_review",
                self.input_path,
                result or self.result,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def input_value(self):
        return {
            "schema_version": 1,
            "acceptance_directory": str(self.acceptance),
            "publication_directory": str(self.publication),
            "child_graph": [
                {
                    "id": "central-child-1",
                    "status": "closed",
                    "dependencies": [
                        {
                            "id": self.request["parent"]["id"],
                            "dependency_type": "parent-child",
                        }
                    ],
                },
                {
                    "id": "central-child-2",
                    "status": "closed",
                    "dependencies": [
                        {
                            "id": self.request["parent"]["id"],
                            "dependency_type": "parent-child",
                        },
                        {"id": "central-child-1", "dependency_type": "blocks"},
                    ],
                },
            ],
            "completions": [
                {
                    "local_id": "implementation",
                    "directory": str(self.completions / "implementation"),
                    "current_subject": {"commit": COMMIT},
                    "terminal": {
                        "kind": "pipeline_run",
                        "directory": str(self.pipeline),
                    },
                },
                {
                    "local_id": "approval",
                    "directory": str(self.completions / "approval"),
                    "current_subject": {"commit": COMMIT},
                    "terminal": {"kind": "completion_record"},
                },
            ],
            "timeout_seconds": 5,
        }

    def write_evidence(self):
        accepted = accept_plan(self.request, self.plan)
        self.acceptance.mkdir()
        (self.acceptance / "input.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "planner_input": self.request,
                    "plan": self.plan,
                }
            )
        )
        (self.acceptance / "output.json").write_text(
            json.dumps(acceptance_output(self.request, accepted))
        )
        self.publication.mkdir()
        (self.publication / "output.json").write_text(
            json.dumps(publication_output(self.request, accepted))
        )
        self.write_pipeline_run()
        for index, (local_id, basis, owner, criteria) in enumerate(
            (
                (
                    "implementation",
                    "pipeline_run",
                    "Example agent",
                    ["criterion-1"],
                ),
                ("approval", "human_attestation", "Brian", ["criterion-2"]),
            ),
            start=1,
        ):
            directory = self.completions / local_id
            directory.mkdir()
            child_id = f"central-child-{index}"
            record = {
                "schema_version": 1,
                "child": child_id,
                "parent_plan": self.plan["plan_sha256"],
                "outcome": "satisfied",
                "producer": {"kind": basis, "identity": owner},
                "criteria": criteria,
                "subject": {"commit": COMMIT},
                "evidence": [f"evidence:{local_id}"],
                "accepted_at": "2026-08-23T00:00:02Z",
            }
            (directory / "input.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "acceptance_directory": str(self.acceptance),
                        "publication_directory": str(self.publication),
                        "expected_subject": {"commit": COMMIT},
                        "record": record,
                    }
                )
            )
            (directory / "output.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "completed",
                        "decision": "satisfied",
                        "source": {"kind": "bead", "id": child_id},
                        "started_at": "2026-08-23T00:00:00Z",
                        "finished_at": "2026-08-23T00:00:01Z",
                        "duration_seconds": 1,
                        "acceptance_sha256": accepted["acceptance_sha256"],
                        "plan_sha256": self.plan["plan_sha256"],
                        "local_id": local_id,
                        "criteria": criteria,
                        "evidence_basis": basis,
                        "satisfies_criteria": True,
                        "record": record,
                        "error_category": None,
                        "artifacts": {"input": "input.json"},
                    }
                )
            )

    def write_pipeline_run(self):
        self.pipeline.mkdir()
        coordinator = self.pipeline / "coordinator"
        coordinator.mkdir()
        history = coordinator_history()
        for path, value in {
            self.pipeline / "bead.json": {
                "schema_version": 1,
                "source": {"kind": "bead", "id": "central-child-1"},
            },
            self.pipeline / "assignment.json": {
                "schema_version": 1,
                "source": {"kind": "bead", "id": "central-child-1"},
            },
            self.pipeline / "preparation.json": {
                "schema_version": 1,
                "bead": {"id": "central-child-1"},
                "preparation_status": "prepared",
                "timestamps": {"finished_at": "2026-08-23T00:00:01Z"},
                "coordinator": {
                    "status": "completed",
                    "exit_code": 0,
                    "outcome": "completed",
                    "decision": "stop",
                    "result": "coordinator/output.json",
                },
            },
            coordinator / "output.json": {
                "schema_version": 1,
                "outcome": "completed",
                "decision": "stop",
                "history": history,
            },
        }.items():
            path.write_text(json.dumps(value))
        change = coordinator / "03-change"
        change.mkdir()
        (change / "output.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "completed",
                    "change": {
                        "objective": "Implement the requested change.",
                        "workspace": str(self.root / "workspace"),
                        "source": {
                            "kind": "attempt",
                            "directory": str(coordinator / "01-attempt"),
                        },
                        "repository": {
                            "before": {
                                "head": "0" * 40,
                                "branch": "fixture",
                                "dirty": False,
                                "status": [],
                            },
                            "after": {
                                "head": COMMIT,
                                "branch": "fixture",
                                "dirty": False,
                                "status": [],
                            },
                        },
                    },
                }
            )
        )


def publication_output(request, accepted):
    return {
        "schema_version": 1,
        "outcome": "completed",
        "decision": "published",
        "source": {"kind": "bead", "id": request["parent"]["id"]},
        "started_at": "2026-08-23T00:00:00Z",
        "finished_at": "2026-08-23T00:00:01Z",
        "duration_seconds": 1,
        "acceptance_sha256": accepted["acceptance_sha256"],
        "plan_sha256": accepted["plan_sha256"],
        "children": [
            {"local_id": child["local_id"], "bead_id": f"central-child-{index}"}
            for index, child in enumerate(accepted["plan"]["children"], start=1)
        ],
        "error_category": None,
        "artifacts": {
            "input": "input.json",
            "stdout": "stdout.log.json",
            "stderr": "stderr.log.json",
        },
    }


def coordinator_history():
    return [
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


def v2_repository_plan():
    request = planner_input()
    request["schema_version"] = 2
    request["parent"]["acceptance_criteria"] = (
        "The caller repository check passes. "
        "The outside-helper repository check passes."
    )
    request["catalog"] = {
        "schema_version": 2,
        "projects": [
            {
                "slug": "example",
                "routes": [
                    {
                        "owner": "Caller agent",
                        "executor": "caller_agent",
                        "evidence_route": "repository_check",
                        "phases": ["implementation"],
                    },
                    {
                        "owner": "Credential holder",
                        "executor": "outside_help",
                        "outside_help_reason": "missing_credentials",
                        "evidence_route": "external_check",
                        "phases": ["closure"],
                    },
                ],
            }
        ],
    }
    request = validate_input(request)
    return request, build_plan(
        request,
        {
            "schema_version": 2,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": "The caller repository check passes.",
                    "statement": "Pass the caller repository check.",
                },
                {
                    "id": "criterion-2",
                    "source_text": "The outside-helper repository check passes.",
                    "statement": "Pass the outside-helper repository check.",
                },
            ],
            "children": [
                {
                    "local_id": "implementation",
                    "title": "Check the implementation repository",
                    "objective": "Run the implementation repository check.",
                    "criteria": ["criterion-1"],
                    "project": "example",
                    "owner": "Caller agent",
                    "phase": "implementation",
                    "executor": "caller_agent",
                    "evidence_route": "repository_check",
                    "depends_on": [],
                },
                {
                    "local_id": "outside-check",
                    "title": "Check the repository with outside help",
                    "objective": "Obtain the outside repository check.",
                    "criteria": ["criterion-2"],
                    "project": "example",
                    "owner": "Credential holder",
                    "phase": "closure",
                    "executor": "outside_help",
                    "outside_help_reason": "missing_credentials",
                    "evidence_route": "external_check",
                    "depends_on": ["implementation"],
                },
            ],
            "ambiguities": [],
        },
    )


def mixed_plan():
    request = planner_input()
    request["parent"]["acceptance_criteria"] = (
        "The change is implemented. The current documentation is approved."
    )
    request["catalog"]["projects"][0]["routes"].append(
        {
            "owner": "Brian",
            "execution": "human",
            "evidence_route": "human_attestation",
            "phases": ["closure"],
        }
    )
    request = validate_input(request)
    return request, build_plan(
        request,
        {
            "schema_version": 1,
            "criteria": [
                {
                    "id": "criterion-1",
                    "source_text": "The change is implemented.",
                    "statement": "Implement the change.",
                },
                {
                    "id": "criterion-2",
                    "source_text": "The current documentation is approved.",
                    "statement": "Approve the current documentation.",
                },
            ],
            "children": [
                {
                    "local_id": "implementation",
                    "title": "Implement the change",
                    "objective": "Implement the requested change.",
                    "criteria": ["criterion-1"],
                    "project": "example",
                    "owner": "Example agent",
                    "phase": "implementation",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                    "depends_on": [],
                },
                {
                    "local_id": "approval",
                    "title": "Approve the documentation",
                    "objective": "Approve the documentation result.",
                    "criteria": ["criterion-2"],
                    "project": "example",
                    "owner": "Brian",
                    "phase": "closure",
                    "execution": "human",
                    "evidence_route": "human_attestation",
                    "depends_on": ["implementation"],
                    "handoff": {
                        "authority": "Brian",
                        "subject_fields": ["commit"],
                        "completion_record": "human_attestation",
                    },
                },
            ],
            "ambiguities": [],
        },
    )


if __name__ == "__main__":
    unittest.main()
