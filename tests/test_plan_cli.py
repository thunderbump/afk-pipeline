import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_plan_agent.py"


class PlanCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_path = self.root / "planner.json"
        self.result = self.root / "result"
        self.request = {
            "schema_version": 1,
            "parent": {
                "id": "central-43zn.33.1",
                "title": "Build Acceptance Planner",
                "description": "Create the first planner component.",
                "acceptance_criteria": "The change is implemented and tested.",
                "labels": ["project:afk-pipeline"],
            },
            "catalog": {
                "schema_version": 1,
                "projects": [
                    {
                        "slug": "afk-pipeline",
                        "routes": [
                            {
                                "owner": "AFK implementation agent",
                                "execution": "agent",
                                "evidence_route": "pipeline_run",
                                "phases": ["implementation"],
                            }
                        ],
                    }
                ],
            },
            "timeout_seconds": 5,
        }

    def test_seals_a_valid_unapproved_route_and_raw_evidence(self):
        completed = self.invoke("valid")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["source"], {"kind": "bead", "id": "central-43zn.33.1"})
        self.assertEqual(output["planner"]["model"], "gpt-5.6-luna")
        self.assertEqual(output["routing"]["decision"], "direct")
        self.assertEqual(
            output["routing"]["routes"][0]["target"],
            {"kind": "source", "id": "central-43zn.33.1"},
        )
        self.assertIsNone(output["plan"])
        self.assertEqual(
            output["artifacts"], {"events": "events.jsonl", "stderr": "stderr.log"}
        )
        self.assertEqual(
            json.loads((self.result / "input.json").read_text()), self.request
        )
        self.assertTrue((self.result / "events.jsonl").stat().st_size > 0)
        self.assertFalse((self.result / "output.json.tmp").exists())

    def test_pipeline_compatible_work_cannot_create_an_unnecessary_child_graph(self):
        completed = self.invoke("unnecessary-decomposition")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["routing"])
        self.assertIsNone(output["plan"])

    def test_direct_result_keeps_the_source_bead_and_has_no_plan_or_children(self):
        completed = self.invoke("direct")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertIsNone(output["plan"])
        self.assertEqual(output["routing"]["decision"], "direct")
        self.assertEqual(output["routing"]["parent"]["id"], "central-43zn.33.1")
        self.assertEqual(len(output["routing"]["parent"]["sha256"]), 64)
        self.assertEqual(
            output["routing"]["routes"],
            [
                {
                    "criterion": "criterion-1",
                    "target": {"kind": "source", "id": "central-43zn.33.1"},
                    "project": "afk-pipeline",
                    "owner": "AFK implementation agent",
                    "phase": "implementation",
                    "execution": "agent",
                    "evidence_route": "pipeline_run",
                }
            ],
        )
        self.assertEqual(output["routing"]["ambiguities"], [])
        self.assertEqual(len(output["routing"]["routing_sha256"]), 64)

    def test_retry_protocol_bug_routes_direct_without_children(self):
        self.request["parent"].update(
            {
                "id": "central-43zn.45",
                "title": "Accept Pi retry event cycles before the final agent terminal",
                "acceptance_criteria": (
                    "Retry event cycles are accepted before the final terminal. "
                    "Repository validation passes."
                ),
            }
        )
        self.request["catalog"]["projects"][0]["routes"].append(
            {
                "owner": "AFK implementation agent",
                "execution": "agent",
                "evidence_route": "repository_check",
                "phases": ["implementation"],
            }
        )

        completed = self.invoke("direct-retry-protocol")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["decision"], "direct")
        self.assertEqual(output["routing"]["parent"]["id"], "central-43zn.45")
        self.assertIsNone(output["plan"])
        self.assertEqual(
            [route["target"] for route in output["routing"]["routes"]],
            [
                {"kind": "source", "id": "central-43zn.45"},
                {"kind": "source", "id": "central-43zn.45"},
            ],
        )

    def test_terminal_decision_change_routes_direct_without_children(self):
        self.request["parent"].update(
            {
                "id": "central-43zn.32",
                "title": "Expose Coordinator terminal decision through Run Preparer",
                "acceptance_criteria": (
                    "The terminal decision is recorded. "
                    "The shared contract is covered by repository validation."
                ),
            }
        )
        self.request["catalog"]["projects"][0]["routes"].append(
            {
                "owner": "AFK implementation agent",
                "execution": "agent",
                "evidence_route": "repository_check",
                "phases": ["implementation"],
            }
        )

        completed = self.invoke("direct-terminal-decision")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["decision"], "direct")
        self.assertEqual(output["routing"]["parent"]["id"], "central-43zn.32")
        self.assertIsNone(output["plan"])

    def test_project_registration_separates_host_closure_from_implementation(self):
        self.request["parent"].update(
            {
                "id": "central-6xx4.1",
                "title": "Register Operations WebUI as a first-class Project",
                "acceptance_criteria": (
                    "The Project and pages are implemented and tested. "
                    "Deployment and served routes are verified."
                ),
                "labels": ["project:operations-webui"],
            }
        )
        self.request["catalog"]["projects"] = [
            {
                "slug": "operations-webui",
                "routes": [
                    {
                        "owner": "Operations implementation agent",
                        "execution": "agent",
                        "evidence_route": "pipeline_run",
                        "phases": ["implementation"],
                    },
                    {
                        "owner": "Host operator",
                        "execution": "external",
                        "evidence_route": "external_check",
                        "phases": ["closure"],
                    },
                ],
            }
        ]

        completed = self.invoke("decompose-project-registration")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["decision"], "decompose")
        self.assertEqual(
            [route["target"]["id"] for route in output["routing"]["routes"]],
            ["implementation", "host-closure"],
        )
        self.assertEqual(
            [child["depends_on"] for child in output["plan"]["children"]],
            [[], ["implementation"]],
        )

    def test_prototype_direction_separates_human_approval(self):
        self.request["parent"].update(
            {
                "id": "central-1m8a",
                "title": "Expose why an AFK Run paused in the Operations Console",
                "acceptance_criteria": (
                    "Presentation options are prototyped. Brian accepts one direction."
                ),
                "labels": ["project:operations-webui"],
            }
        )
        self.request["catalog"]["projects"] = [
            {
                "slug": "operations-webui",
                "routes": [
                    {
                        "owner": "Operations prototype agent",
                        "execution": "agent",
                        "evidence_route": "pipeline_run",
                        "phases": ["implementation"],
                    },
                    {
                        "owner": "Brian",
                        "execution": "human",
                        "evidence_route": "human_attestation",
                        "phases": ["closure"],
                    },
                ],
            }
        ]

        completed = self.invoke("decompose-human-approval")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["decision"], "decompose")
        self.assertEqual(
            [child["execution"] for child in output["plan"]["children"]],
            ["agent", "human"],
        )
        self.assertEqual(output["plan"]["children"][1]["depends_on"], ["prototype"])

    def test_exporter_work_splits_at_repository_and_host_boundaries(self):
        self.request["parent"].update(
            {
                "id": "central-43zn.37",
                "title": "Export portable v2 Run artifacts and paused terminals",
                "acceptance_criteria": (
                    "The AFK exporter is implemented and tested. "
                    "Operations documentation is published. "
                    "The documented site is deployed and verified."
                ),
            }
        )
        self.request["catalog"]["projects"] = [
            {
                "slug": "afk-pipeline",
                "routes": [
                    {
                        "owner": "AFK implementation agent",
                        "execution": "agent",
                        "evidence_route": "pipeline_run",
                        "phases": ["implementation"],
                    }
                ],
            },
            {
                "slug": "operations-webui",
                "routes": [
                    {
                        "owner": "Operations documentation agent",
                        "execution": "agent",
                        "evidence_route": "pipeline_run",
                        "phases": ["closure"],
                    },
                    {
                        "owner": "Host operator",
                        "execution": "external",
                        "evidence_route": "external_check",
                        "phases": ["closure"],
                    },
                ],
            },
        ]

        completed = self.invoke("decompose-exporter-closure")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["routing"]["decision"], "decompose")
        self.assertEqual(
            [route["target"]["id"] for route in output["routing"]["routes"]],
            ["afk-implementation", "operations-docs", "host-closure"],
        )
        self.assertEqual(
            [child["depends_on"] for child in output["plan"]["children"]],
            [[], ["afk-implementation"], ["operations-docs"]],
        )

    def test_invalid_proposal_seals_failed_output_without_a_plan(self):
        completed = self.invoke("invalid-proposal")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertEqual(output["error_category"], "invalid_proposal")
        self.assertIsNone(output["routing"])
        self.assertIsNone(output["plan"])

    def test_process_failure_cannot_publish_a_plan(self):
        completed = self.invoke("process-failure")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["error_category"], "agent_process")
        self.assertIsNone(output["routing"])
        self.assertIsNone(output["plan"])
        self.assertEqual(output["process"]["exit_code"], 7)

    def test_invalid_events_seal_an_agent_protocol_failure(self):
        completed = self.invoke("invalid-events")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((self.result / "output.json").read_text())
        self.assertEqual(output["error_category"], "agent_protocol")
        self.assertIsNone(output["routing"])
        self.assertIsNone(output["plan"])

    def test_invalid_input_and_existing_result_exit_two_without_mutation(self):
        self.request["parent"]["acceptance_criteria"] = ""
        invalid = self.invoke("valid")
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse(self.result.exists())

        self.request["parent"]["acceptance_criteria"] = (
            "The change is implemented and tested."
        )
        self.result.mkdir()
        existing = self.invoke("valid")
        self.assertEqual(existing.returncode, 2)
        self.assertEqual(list(self.result.iterdir()), [])

    def test_help_is_available_without_input(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_plan", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("PLANNER_JSON RESULT_DIRECTORY", completed.stdout)

    def invoke(self, scenario):
        self.input_path.write_text(json.dumps(self.request))
        environment = os.environ.copy()
        environment["AFK_PLAN_AGENT_COMMAND"] = json.dumps(
            [sys.executable, str(FIXTURE), scenario]
        )
        return subprocess.run(
            [sys.executable, "-m", "afk_plan", self.input_path, self.result],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
