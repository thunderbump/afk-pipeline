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
from unittest import mock

from afk_related_work import build_snapshot, reference
from tests.inference_cli_fixture import install_pi

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixture_review_agent.py"
ASSESSMENT_FIXTURE = ROOT / "tests" / "fixture_assessment_agent.py"


class PublicReviewCliTest(unittest.TestCase):
    def test_help_exits_zero_and_describes_arguments(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_review", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "usage: python3 -m afk_review REVIEW_JSON RESULT_DIRECTORY",
            completed.stdout,
        )
        self.assertIn("review JSON", completed.stdout)
        self.assertIn("RESULT_DIRECTORY", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_wrong_number_of_ordinary_arguments_exits_two(self):
        completed = subprocess.run(
            [sys.executable, "-m", "afk_review", "review.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "usage: python3 -m afk_review REVIEW_JSON RESULT_DIRECTORY",
            completed.stderr,
        )


class ReviewCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.name", "AFK Test")
        self.git("config", "user.email", "afk-test@example.invalid")
        (self.workspace / "README.md").write_text("before\n")
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "note.txt").write_text("tracked directory\n")
        self.git("add", "README.md", "docs/note.txt")
        self.git("commit", "--quiet", "-m", "Initial state")
        self.before = self.state()
        (self.workspace / "README.md").write_text("after\n")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "Implementation")
        self.after = self.state()
        self.change = self.root / "committed-change"
        self.change.mkdir()
        self.write_json(
            self.change / "input.json",
            {
                "schema_version": 1,
                "source": {
                    "kind": "attempt",
                    "directory": str(self.root / "attempt"),
                },
            },
        )
        self.write_json(
            self.change / "output.json",
            {
                "schema_version": 1,
                "outcome": "completed",
                "change": {
                    "objective": "Change before to after.",
                    "workspace": str(self.workspace),
                    "repository": {"before": self.before, "after": self.after},
                    "source": {
                        "kind": "attempt",
                        "directory": str(self.root / "attempt"),
                    },
                },
            },
        )
        self.validation = self.root / "validation"
        self.validation.mkdir()
        self.write_json(
            self.validation / "input.json",
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "command": ["python3", "-m", "unittest"],
                "timeout_seconds": 30,
            },
        )
        self.write_json(
            self.validation / "output.json",
            {
                "schema_version": 1,
                "outcome": "passed",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1.0,
                "process": {"exit_code": 0, "signal": None},
                "repository": {
                    "before": self.after,
                    "after": self.after,
                    "head_changed": False,
                },
                "artifacts": {"stdout": "stdout.log", "stderr": "stderr.log"},
            },
        )
        (self.validation / "stdout.log").write_text("Ran 12 tests - OK\n")
        (self.validation / "stderr.log").write_text("validation warning\n")

    def test_completed_review_seals_structured_output_and_raw_artifacts(self):
        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        for line in completed.stdout.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T.*Z ")
        self.assertEqual(
            [line.split(" ", 1)[1] for line in completed.stdout.splitlines()],
            [
                "loading review input",
                "review input accepted",
                "loading Committed Change and Validation evidence",
                "observing reviewed repository",
                "preparing review result directory",
                (
                    "starting review agent "
                    f"(timeout=5s; artifacts: events={result / 'events.jsonl'}, "
                    f"stderr={result / 'stderr.log'})"
                ),
                "review agent completed",
                "observing repository after review",
                f"sealed completed review outcome at {result / 'output.json'}",
            ],
        )
        self.assertNotIn("No actionable defects", completed.stdout)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["process"], {"exit_code": 0, "signal": None})
        self.assertEqual(output["agent"], {"status": "completed"})
        self.assertEqual(
            output["review"],
            {
                "summary": "No actionable defects found.",
                "findings": [],
                "audit": {
                    "completed": True,
                    "scopes": [
                        "objective",
                        "acceptance_criteria",
                        "reviewed_diff",
                        "supplied_evidence",
                    ],
                },
            },
        )
        self.assertEqual(output["repository"]["before"], self.after)
        self.assertEqual(output["repository"]["after"], self.after)
        self.assertTrue(output["repository"]["unchanged"])
        self.assertEqual(output["artifacts"]["diff"], "diff.patch")
        diff = (result / "diff.patch").read_text()
        self.assertIn("-before", diff)
        self.assertIn("+after", diff)
        self.assertTrue((result / "events.jsonl").is_file())
        self.assertEqual((result / "stderr.log").read_text(), "")
        receipt = json.loads((result / "inference/receipt.json").read_text())
        invocation = json.loads((result / "inference/invocation.json").read_text())
        self.assertEqual(receipt["policy"]["requested_capability"], "READ_ONLY")
        self.assertEqual(invocation["execution_root"], str(self.workspace))
        self.assertFalse((result / "output.json.tmp").exists())

    def test_split_review_runs_three_isolated_lenses_and_seals_provenance(self):
        (self.validation / "stdout.log").write_text("large diagnostic line\n" * 4096)
        input_path, result, environment = self.prepare_review("no-findings")
        review_input = json.loads(input_path.read_text())
        review_input["review_mode"] = "split"
        self.write_json(input_path, review_input)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["review_mode"], "split")
        self.assertEqual(
            [item["lens"] for item in output["review_invocations"]],
            ["behavior", "design", "standards"],
        )
        self.assertEqual(output["finding_provenance"], [])
        self.assertEqual(
            output["review"]["summary"],
            "Behavior: No actionable defects found.\n"
            "Design: No actionable defects found.\n"
            "Standards: No actionable defects found.",
        )
        self.assertNotIn("process", output)
        self.assertEqual(output["artifacts"], {"diff": "diff.patch"})
        for lens in ("behavior", "design", "standards"):
            directory = result / "reviewers" / lens
            self.assertTrue((directory / "inference" / "receipt.json").is_file())
            prompt = json.loads((directory / "inference" / "prompt.json").read_text())[
                "trusted_task_instructions"
            ]
            packet = json.loads((directory / "inference" / "prompt.json").read_text())
            reference = packet["untrusted_task_data"]["validation"]["stdout"]
            self.assertEqual(reference["path"], str(self.validation / "stdout.log"))
            self.assertIn(reference["path"], packet["system"])
            self.assertLess(
                (directory / "inference/invocation.json").stat().st_size, 65536
            )
            self.assertIn(f"isolated {lens} lens", prompt)
            for other in {"behavior", "design", "standards"} - {lens}:
                self.assertNotIn(f"{other.capitalize()} lens:", prompt)

        # Exercise the public consumer seam with the fixture-generated receipts,
        # rather than validating only a hand-built descriptive matrix.
        assessment_input = self.root / "assessment.json"
        self.write_json(
            assessment_input,
            {
                "schema_version": 1,
                "workspace": str(self.workspace),
                "review_directory": str(result),
                "timeout_seconds": 5,
            },
        )
        assessment_result = self.root / "assessment"
        install_pi(self.root / "bin", ASSESSMENT_FIXTURE, "no-findings")
        assessed = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk_assess",
                str(assessment_input),
                str(assessment_result),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(assessed.returncode, 0, assessed.stderr)
        self.assertEqual(
            json.loads((assessment_result / "output.json").read_text())["assessment"][
                "decisions"
            ],
            [],
        )

    def test_complete_validation_evidence_is_embedded_for_read_only_review(self):
        result, completed = self.run_review("validation-evidence")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"], "completed"
        )

    def test_feedback_response_change_uses_the_same_review_interface(self):
        change = json.loads((self.change / "output.json").read_text())
        change["change"]["source"] = {
            "kind": "feedback_response",
            "directory": str(self.root / "response"),
        }
        self.write_json(self.change / "output.json", change)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads((result / "input.json").read_text())["change_directory"],
            str(self.change),
        )
        self.assertIn("-before", (result / "diff.patch").read_text())

    def test_findings_are_completed_and_require_line_anchors(self):
        result, completed = self.run_review("multiple-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(len(output["review"]["findings"]), 2)
        self.assertEqual(
            output["review"]["findings"][0]["locations"],
            [{"path": "README.md", "line": 1}],
        )
        self.assertEqual(
            output["review"]["audit"],
            {
                "completed": True,
                "scopes": [
                    "objective",
                    "acceptance_criteria",
                    "reviewed_diff",
                    "supplied_evidence",
                ],
            },
        )

        result, completed = self.run_review("missing-line", result_name="invalid")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["review"])
        self.assertIn("location fields are malformed", output["review_error"])

    def test_finding_locations_must_exist_within_the_reviewed_head(self):
        (self.workspace / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (self.workspace / "ignored.txt").write_text("not in HEAD\n")
        for scenario in (
            "missing-path",
            "outside-path",
            "ignored-path",
            "directory-path",
            "bad-line",
        ):
            with self.subTest(scenario=scenario):
                result, completed = self.run_review(scenario, result_name=scenario)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["review"])
                self.assertIn("finding location", output["review_error"])

    def test_workspace_must_match_the_validated_change_before_review(self):
        (self.workspace / "unreviewed.txt").write_text("different state\n")

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("workspace must match", completed.stderr)

    def test_dirty_implementation_evidence_is_refused_before_review(self):
        (self.workspace / "uncommitted.txt").write_text("not committed\n")
        dirty = self.state()
        change = json.loads((self.change / "output.json").read_text())
        change["change"]["repository"]["after"] = dirty
        self.write_json(self.change / "output.json", change)
        validation = json.loads((self.validation / "output.json").read_text())
        validation["repository"]["before"] = dirty
        validation["repository"]["after"] = dirty
        self.write_json(self.validation / "output.json", validation)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("repository states must be clean", completed.stderr)

    def test_detached_workspace_at_the_validated_head_is_reviewable(self):
        self.git("checkout", "--quiet", "--detach")

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertIsNone(output["repository"]["before"]["branch"])
        self.assertTrue(output["repository"]["unchanged"])

    def test_validation_branch_only_transition_is_reviewable(self):
        validation = json.loads((self.validation / "output.json").read_text())
        validation["repository"]["after"] = {
            **validation["repository"]["after"],
            "branch": None,
        }
        self.write_json(self.validation / "output.json", validation)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "completed")
        self.assertTrue(output["repository"]["unchanged"])

    def test_reviewer_workspace_mutation_is_a_sealed_failure(self):
        result, completed = self.run_review("mutate-workspace")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertFalse(output["repository"]["unchanged"])
        self.assertTrue(output["repository"]["after"]["dirty"])

    def test_interrupt_between_split_lenses_seals_partial_stage_evidence(self):
        input_path, result, environment = self.prepare_review("no-findings")
        review_input = json.loads(input_path.read_text())
        review_input["review_mode"] = "split"
        self.write_json(input_path, review_input)

        from afk_review import __main__ as review_main

        actual_build_task = review_main.build_task

        def interrupt_design(*args, **kwargs):
            if kwargs.get("lens") == "design":
                raise KeyboardInterrupt
            return actual_build_task(*args, **kwargs)

        with (
            mock.patch.object(review_main, "build_task", side_effect=interrupt_design),
            mock.patch.object(
                sys, "argv", ["afk_review", str(input_path), str(result)]
            ),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            returncode = review_main.main()

        self.assertEqual(returncode, 1)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "interrupted")
        self.assertIsNone(output["review"])
        self.assertEqual(
            [item["lens"] for item in output["review_invocations"]], ["behavior"]
        )
        self.assertEqual(output["review_invocations"][0]["outcome"], "succeeded")
        self.assertTrue(
            (result / "reviewers/behavior/inference/receipt.json").is_file()
        )
        self.assertIsNone(output["repository"]["unchanged"])

    def test_split_interruption_boundaries_and_failed_shutdown(self):
        from afk_export import failed_review_details
        from afk_review import __main__ as review_main
        from afk_review.contract import validate_invocation_receipts

        for boundary in ("first", "finalize", "after_seal", "shutdown_failure"):
            with self.subTest(boundary=boundary):
                input_path, result, environment = self.prepare_review(
                    "no-findings", result_name=boundary
                )
                request = json.loads(input_path.read_text())
                request["review_mode"] = "split"
                self.write_json(input_path, request)
                original_progress = review_main.progress

                def progress(
                    message, boundary=boundary, original_progress=original_progress
                ):
                    if (
                        boundary == "finalize"
                        and message == "observing repository after review"
                    ) or (
                        boundary == "after_seal"
                        and message.startswith("sealed completed")
                    ):
                        raise KeyboardInterrupt
                    original_progress(message)

                with (
                    mock.patch.object(
                        sys, "argv", ["afk_review", str(input_path), str(result)]
                    ),
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.object(review_main, "progress", side_effect=progress),
                ):
                    if boundary in {"first", "shutdown_failure"}:
                        with mock.patch.object(
                            review_main, "build_task", side_effect=KeyboardInterrupt
                        ):
                            if boundary == "shutdown_failure":
                                with (
                                    mock.patch.object(
                                        review_main,
                                        "seal_json",
                                        side_effect=OSError("disk unavailable"),
                                    ),
                                    self.assertRaises(OSError),
                                ):
                                    review_main.main()
                                self.assertFalse((result / "output.json").exists())
                                continue
                            code = review_main.main()
                    else:
                        code = review_main.main()
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(code, 0 if boundary == "after_seal" else 1)
                self.assertEqual(
                    output["outcome"],
                    "completed" if boundary == "after_seal" else "interrupted",
                )
                if boundary != "after_seal":
                    self.assertIsNone(output["review"])
                    validate_invocation_receipts(output, result)
                    # Empty and completed prefixes remain accepted by the export projection.
                    details = failed_review_details(output, [])
                    self.assertEqual(details["mode"], "split")

    def test_final_split_lens_mutation_never_publishes_an_aggregate(self):
        input_path, result, environment = self.prepare_review("mutate-final-lens")
        review_input = json.loads(input_path.read_text())
        review_input["review_mode"] = "split"
        self.write_json(input_path, review_input)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["review"])
        self.assertIsNone(output["agent"])
        self.assertFalse(output["repository"]["unchanged"])
        self.assertEqual(len(output["review_invocations"]), 3)
        self.assertTrue(
            all(item["outcome"] == "succeeded" for item in output["review_invocations"])
        )
        self.assertEqual(output["finding_provenance"], [])

    def test_agent_protocol_and_post_review_observation_failures_are_sealed(self):
        result, completed = self.run_review("invalid-events")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["agent"])
        self.assertEqual(
            json.loads((result / "inference/receipt.json").read_text())["protocol"][
                "status"
            ],
            "protocol_malformed",
        )

        result, completed = self.run_review("damage-git", result_name="damaged-git")

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertIsNone(output["repository"]["after"])
        self.assertIn("observation_error", output["repository"])

    def test_malformed_assistant_content_shapes_are_sealed_protocol_failures(self):
        for scenario in ("null-content", "object-content", "invalid-text-part"):
            with self.subTest(scenario=scenario):
                result, completed = self.run_review(scenario, result_name=scenario)

                self.assertEqual(completed.returncode, 1, completed.stderr)
                output = json.loads((result / "output.json").read_text())
                self.assertEqual(output["outcome"], "failed")
                self.assertIsNone(output["agent"])
                receipt = json.loads((result / "inference/receipt.json").read_text())
                self.assertEqual(receipt["protocol"]["status"], "protocol_malformed")
                self.assertIsNone(output["review"])
                self.assertTrue(output["repository"]["unchanged"])
                self.assertFalse((result / "output.json.tmp").exists())

    def test_timeout_terminates_the_agent_process_group_and_seals_output(self):
        marker = self.root / "descendant.pid"

        result, completed = self.run_review(
            "hang", timeout_seconds=1, extra_args=[str(marker)]
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "timed_out")
        descendant = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_review_queries_sibling_ownership_without_prompt_injection(self):
        records = {
            "task": {"id": "task", "title": "Change the API", "parent": "epic"},
            "epic": {
                "id": "epic",
                "title": "API epic",
                "children": ["task", "callers"],
            },
            "callers": {
                "id": "callers",
                "title": "Migrate callers",
                "description": "Caller migration is owned by this sibling.",
            },
        }
        raw, facts = build_snapshot(records["task"], records.__getitem__)
        snapshot = self.root / "related-work.jsonl"
        snapshot.write_bytes(raw)
        input_path, result, environment = self.prepare_review("sibling-owned-migration")
        review_input = json.loads(input_path.read_text())
        review_input["related_work"] = reference(snapshot, facts)
        self.write_json(input_path, review_input)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        finding = output["review"]["findings"][0]
        self.assertEqual(finding["lens"], "behavior")
        self.assertEqual(
            finding["scope_claim"],
            {
                "kind": "related",
                "rationale": "The frozen sibling record owns caller migration.",
                "related_work_id": "callers",
            },
        )
        self.assertIn("related sibling", output["review"]["summary"])

    def test_agent_launch_failure_is_sealed(self):
        result, completed = self.run_review(
            "unused", command=[str(self.root / "missing-agent")]
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        output = json.loads((result / "output.json").read_text())
        self.assertEqual(output["outcome"], "failed")
        self.assertNotEqual(output["process"]["exit_code"], 0)
        self.assertIsNone(output["agent"])

    def test_interrupt_terminates_the_agent_process_group_and_seals_output(self):
        marker = self.root / "interrupted-descendant.pid"
        input_path, result, environment = self.prepare_review(
            "hang", timeout_seconds=30, extra_args=[str(marker)]
        )
        reviewer = subprocess.Popen(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.01)
        self.assertTrue(marker.is_file())

        reviewer.send_signal(signal.SIGINT)
        _stdout, stderr = reviewer.communicate(timeout=5)

        self.assertEqual(reviewer.returncode, 1, stderr)
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "interrupted",
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_closed_progress_stdout_does_not_prevent_sealing(self):
        input_path, result, environment = self.prepare_review("delayed-no-findings")
        reviewer = subprocess.Popen(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert reviewer.stdout is not None
        for line in reviewer.stdout:
            if "starting review agent" in line:
                break
        else:
            self.fail("review agent did not start")
        reviewer.stdout.close()
        assert reviewer.stderr is not None
        stderr = reviewer.stderr.read()
        reviewer.stderr.close()

        self.assertEqual(reviewer.wait(timeout=5), 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads((result / "output.json").read_text())["outcome"],
            "completed",
        )

    def test_invalid_input_and_existing_result_are_refused(self):
        input_path, result, environment = self.prepare_review("no-findings")
        value = json.loads(input_path.read_text())
        value["timeout_seconds"] = 0
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())

        input_path, result, environment = self.prepare_review("no-findings")
        result.mkdir()
        sentinel = result / "keep.txt"
        sentinel.write_text("caller data\n")

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual([sentinel], list(result.iterdir()))

    def test_attempt_directory_is_not_a_supported_review_input(self):
        input_path, result, environment = self.prepare_review("no-findings")
        value = json.loads(input_path.read_text())
        value["attempt_directory"] = value.pop("change_directory")
        self.write_json(input_path, value)

        completed = self.invoke(input_path, result, environment)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("change_directory", completed.stderr)
        self.assertFalse(result.exists())

    def test_failed_and_mismatched_change_or_validation_are_refused(self):
        original_change = json.loads((self.change / "output.json").read_text())
        original_validation = json.loads((self.validation / "output.json").read_text())
        cases = {
            "failed-change": (
                lambda change, _validation: change.update(outcome="failed"),
                "Committed Change must have completed",
            ),
            "failed-validation": (
                lambda _change, validation: validation.update(outcome="failed"),
                "invalid passed Validation output",
            ),
            "wrong-validation-before": (
                lambda _change, validation: validation["repository"].update(
                    before=self.before
                ),
                "passed Validation changed the repository",
            ),
            "dirty-change-before": (
                lambda change, _validation: change["change"]["repository"][
                    "before"
                ].update(dirty=True, status=[" M README.md"]),
                "repository states must be clean",
            ),
        }
        for name, (mutate, error) in cases.items():
            with self.subTest(name=name):
                change = json.loads(json.dumps(original_change))
                validation = json.loads(json.dumps(original_validation))
                mutate(change, validation)
                self.write_json(self.change / "output.json", change)
                self.write_json(self.validation / "output.json", validation)

                result, completed = self.run_review(
                    "no-findings", result_name=f"review-{name}"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(error, completed.stderr)
                self.assertFalse(result.exists())

    def test_noncanonical_or_missing_change_heads_are_refused_before_results(self):
        original = json.loads((self.change / "output.json").read_text())
        for name, head in (
            ("symbolic", "HEAD~1"),
            ("missing", "0000000000000000000000000000000000000000"),
        ):
            with self.subTest(name=name):
                change = json.loads(json.dumps(original))
                change["change"]["repository"]["before"]["head"] = head
                self.write_json(self.change / "output.json", change)

                result, completed = self.run_review(
                    "no-findings", result_name=f"review-{name}-head"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn("canonical commit object IDs", completed.stderr)
                self.assertFalse(result.exists())

    def test_malformed_evidence_is_refused_before_result_creation(self):
        self.write_json(self.change / "output.json", {"outcome": "completed"})

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("Committed Change must use schema_version 1", completed.stderr)

    def test_committed_change_provenance_is_required(self):
        change = json.loads((self.change / "output.json").read_text())
        del change["change"]["source"]
        self.write_json(self.change / "output.json", change)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("Committed Change source is invalid", completed.stderr)

    def test_validation_schema_is_required(self):
        validation = json.loads((self.validation / "output.json").read_text())
        del validation["schema_version"]
        self.write_json(self.validation / "output.json", validation)

        result, completed = self.run_review("no-findings")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("invalid passed Validation output", completed.stderr)

    def test_malformed_or_mismatched_validation_is_rejected_before_review(self):
        original_input = json.loads((self.validation / "input.json").read_text())
        original_output = json.loads((self.validation / "output.json").read_text())
        cases = {
            "wrong-workspace": (
                lambda value: value.update(workspace=str(self.root / "other")),
                None,
                "Review workspace must match Validation",
            ),
            "empty-command": (
                lambda value: value.update(command=[]),
                None,
                "invalid passed Validation input",
            ),
            "nonzero-process": (
                None,
                lambda value: value.update(process={"exit_code": 1, "signal": None}),
                "invalid passed Validation output",
            ),
            "wrong-artifacts": (
                None,
                lambda value: value.update(
                    artifacts={"stdout": "other.log", "stderr": "stderr.log"}
                ),
                "logs are not identified",
            ),
        }
        for name, (mutate_input, mutate_output, error) in cases.items():
            with self.subTest(name=name):
                validation_input = json.loads(json.dumps(original_input))
                validation_output = json.loads(json.dumps(original_output))
                if mutate_input is not None:
                    mutate_input(validation_input)
                if mutate_output is not None:
                    mutate_output(validation_output)
                self.write_json(self.validation / "input.json", validation_input)
                self.write_json(self.validation / "output.json", validation_output)

                result, completed = self.run_review(
                    "no-findings", result_name=f"review-{name}"
                )

                self.assertEqual(completed.returncode, 2)
                self.assertFalse(result.exists())
                self.assertIn(error, completed.stderr)

        self.write_json(self.validation / "input.json", original_input)
        self.write_json(self.validation / "output.json", original_output)
        (self.validation / "stdout.log").unlink()
        (self.validation / "stdout.log").symlink_to(self.validation / "stderr.log")

        result, completed = self.run_review(
            "no-findings", result_name="review-symlink-log"
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(result.exists())
        self.assertIn("logs are unavailable", completed.stderr)

    def run_review(
        self,
        scenario,
        result_name="review",
        timeout_seconds=5,
        extra_args=None,
        command=None,
    ):
        input_path, result, environment = self.prepare_review(
            scenario,
            result_name=result_name,
            timeout_seconds=timeout_seconds,
            extra_args=extra_args,
            command=command,
        )
        return result, self.invoke(input_path, result, environment)

    def prepare_review(
        self,
        scenario,
        result_name="review",
        timeout_seconds=5,
        extra_args=None,
        command=None,
    ):
        review = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "change_directory": str(self.change),
            "validation_directory": str(self.validation),
            "timeout_seconds": timeout_seconds,
        }
        input_path = self.root / "review.json"
        self.write_json(input_path, review)
        result = self.root / result_name
        environment = os.environ.copy()
        bin_directory = self.root / "bin"
        bin_directory.mkdir(exist_ok=True)
        if command is None:
            install_pi(bin_directory, FIXTURE, scenario, *(extra_args or []))
        else:
            executable = bin_directory / "pi"
            rendered = " ".join(shlex.quote(item) for item in command)
            executable.write_text(f'#!/bin/sh\nexec {rendered} "$@"\n')
            executable.chmod(0o755)
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        return input_path, result, environment

    def invoke(self, input_path, result, environment):
        return subprocess.run(
            [sys.executable, "-m", "afk_review", str(input_path), str(result)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def state(self):
        status = self.git("status", "--porcelain").splitlines()
        return {
            "head": self.git("rev-parse", "HEAD"),
            "branch": "main",
            "dirty": bool(status),
            "status": status,
        }

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
