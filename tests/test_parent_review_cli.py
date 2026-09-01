import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk_plan.contract import build_plan, validate_input
from afk_plan_accept.contract import accept_plan
from tests.inference_cli_fixture import install_pi
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
        self.request, _ = v2_repository_plan()
        fixture = ROOT / "tests" / "fixture_plan_agent.py"
        fake_bd = ROOT / "tests" / "fixtures" / "fake_bd.py"

        planner_input_path = self.root / "capability-planner.json"
        planner_result = self.root / "capability-planner"
        planner_input_path.write_text(json.dumps(self.request))
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        install_pi(bin_directory, fixture, "capability-fan-in")
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        planned = self.run_cli(
            "afk_plan", planner_input_path, planner_result, environment
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.plan = json.loads((planner_result / "output.json").read_text())["plan"]

        acceptance_input = self.root / "capability-acceptance.json"
        self.acceptance = self.root / "capability-acceptance"
        acceptance_input.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "planner_input": self.request,
                    "plan": self.plan,
                }
            )
        )
        accepted = self.run_cli("afk_plan_accept", acceptance_input, self.acceptance)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        beads = self.root / "capability-beads"
        beads.mkdir()
        state_path = beads / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "parent": {
                        **self.request["parent"],
                        "status": "in_progress",
                        "issue_type": "task",
                        "priority": 2,
                        "dependencies": [],
                    },
                    "children": [],
                }
            )
        )
        publication_input = self.root / "capability-publication.json"
        self.publication = self.root / "capability-publication"
        publication_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acceptance_directory": str(self.acceptance),
                    "beads_workspace": str(beads),
                    "command": [sys.executable, str(fake_bd), str(state_path)],
                    "timeout_seconds": 30,
                }
            )
        )
        published = self.run_cli(
            "afk_plan_publish", publication_input, self.publication
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        publication = json.loads((self.publication / "output.json").read_text())
        mappings = {
            item["local_id"]: item["bead_id"] for item in publication["children"]
        }

        self.completions = self.root / "capability-completions"
        self.completions.mkdir()
        completion_requests = []
        for child in self.plan["children"]:
            child_id = mappings[child["local_id"]]
            completion_input = self.root / f"complete-{child['local_id']}.json"
            completion = self.completions / child["local_id"]
            completion_input.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "acceptance_directory": str(self.acceptance),
                        "publication_directory": str(self.publication),
                        "expected_subject": {"commit": COMMIT},
                        "record": {
                            "schema_version": 1,
                            "child": child_id,
                            "parent_plan": self.plan["plan_sha256"],
                            "outcome": "satisfied",
                            "producer": {
                                "kind": child["evidence_route"],
                                "identity": child["owner"],
                            },
                            "criteria": child["criteria"],
                            "subject": {"commit": COMMIT},
                            "evidence": [f"repository-check:{child['local_id']}"],
                            "accepted_at": "2026-08-23T00:00:02Z",
                        },
                    }
                )
            )
            completed = self.run_cli("afk_complete", completion_input, completion)
            self.assertEqual(completed.returncode, 0, completed.stderr)

            terminal = {"kind": "completion_record"}
            if child["evidence_route"] == "repository_check":
                check = self.write_repository_check(child["local_id"])
                terminal = {"kind": "repository_check", "directory": str(check)}
            completion_requests.append(
                {
                    "local_id": child["local_id"],
                    "directory": str(completion),
                    "current_subject": {"commit": COMMIT},
                    "terminal": terminal,
                }
            )
            closed = subprocess.run(
                [
                    sys.executable,
                    str(fake_bd),
                    str(state_path),
                    "close",
                    child_id,
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(closed.returncode, 0, closed.stderr)

        state = json.loads(state_path.read_text())
        review_request = {
            "schema_version": 1,
            "acceptance_directory": str(self.acceptance),
            "publication_directory": str(self.publication),
            "child_graph": [
                {key: child[key] for key in ("id", "status", "dependencies")}
                for child in state["children"]
            ],
            "completions": completion_requests,
            "timeout_seconds": 5,
        }
        reviewed = self.invoke("accepted", review_request)

        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        fan_in = json.loads((self.result / "fan-in.json").read_text())
        self.assertEqual(
            [child["executor"] for child in fan_in["children"]],
            ["caller_agent", "outside_help"],
        )
        self.assertTrue(all("execution" not in child for child in fan_in["children"]))
        self.assertEqual(
            [child["terminal"]["status"] for child in fan_in["children"]],
            ["passed", "validated"],
        )
        outside = next(
            child for child in fan_in["children"] if child["executor"] == "outside_help"
        )
        self.assertEqual(outside["evidence_basis"], "external_check")

    def run_cli(self, module, input_path, result, environment=None):
        return subprocess.run(
            [sys.executable, "-m", module, input_path, result],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_repository_check(self, local_id):
        check = self.root / f"repository-check-{local_id}"
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
        return check

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

    def test_retired_approval_record_is_rejected_by_parent_contract(self):
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

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertFalse(self.result.exists())

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

    def test_semantic_runtime_retains_receipt_and_terminal_response(self):
        self.input_path.write_text(json.dumps(self.input_value()))
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        install_pi(bin_directory, FIXTURE, "accepted")
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

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

        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads((self.result / "inference/receipt.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "NO_TOOLS")
        self.assertIsNotNone(receipt["terminal_response"])

    def invoke(self, scenario, request=None, result=None):
        self.input_path.write_text(json.dumps(request or self.input_value()))
        bin_directory = self.root / "bin"
        bin_directory.mkdir(exist_ok=True)
        install_pi(bin_directory, FIXTURE, scenario)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
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
                ("approval", "external_check", "Brian", ["criterion-2"]),
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
            "execution": "external",
            "evidence_route": "external_check",
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
                    "execution": "external",
                    "evidence_route": "external_check",
                    "depends_on": ["implementation"],
                    "handoff": {
                        "authority": "Brian",
                        "subject_fields": ["commit"],
                        "completion_record": "external_check",
                    },
                },
            ],
            "ambiguities": [],
        },
    )


if __name__ == "__main__":
    unittest.main()
