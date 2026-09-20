"""Driver boundaries exercised against the independent status decision contract."""

import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from afk_orchestrate import __main__ as cli
from afk_orchestrate import driver
from afk_pr import decision
from afk_pr.__main__ import main as pr_main
from tests.test_pr_decision import BASE, HEAD, OLD, URL, context, fixture, review


class Crash(BaseException):
    pass


class World:
    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.jobs = {}
        self.head = HEAD
        self.base = BASE
        self.findings = []
        self.crash = None
        self.number = 2
        self.creation = self.make_job("1" * 16, "creation", OLD)
        self.creation["phases"] = {
            "creation": {
                "state": "completed",
                "publication": "published",
                "progress": {
                    "push": "pushed",
                    "candidate": HEAD,
                    "pr_url": URL,
                    "fixture_job": "f" * 16,
                },
            }
        }
        self.creation["job"]["bead_id"] = "central-example"
        child = self.make_job("f" * 16, "review", HEAD)
        child["job"].update(expected_phases=["fixtures"], creation_job="1" * 16)
        child["phases"] = {"fixtures": fixture()}

    def make_job(self, identifier, kind, head):
        item = {
            "job": {
                "id": identifier,
                "pr_url": URL,
                "head": head,
                "base": self.base,
                "kind": kind,
                "expected_phases": ["fixtures", "review"]
                if kind == "review"
                else [kind],
            },
            "phases": {},
        }
        self.jobs[identifier] = item
        return item

    def __call__(self, *args):
        self.calls.append(args)
        command = args[0]
        if command == "pr":
            result = self.creation
        elif command == "job":
            result = self.jobs[args[1]]
        elif command == "context":
            result = context()
            result["pull_request"]["head"]["sha"] = self.head
            result["pull_request"]["base"]["sha"] = self.base
        elif command == "status":
            item = self.jobs[args[3]]
            records = [item]
            for phase in item["phases"].values():
                child = phase.get("progress", {}).get("fixture_job")
                if child and child in self.jobs:
                    records.append(self.jobs[child])
            observed = self("context", URL)
            result = {
                "head": self.head,
                "jobs": records,
                "decision": decision.decide(observed, records),
            }
        elif command in {"review", "respond"}:
            action_id = args[3]
            if action_id in self.receipts:
                return copy.deepcopy(self.receipts[action_id])
            assert args[5] == self.head
            identifier = f"{self.number:016x}"
            self.number += 1
            item = self.make_job(
                identifier, "review" if command == "review" else "response", self.head
            )
            item["job"]["action_id"] = action_id
            item["action"] = {
                "id": action_id,
                "command": command,
                "job_id": identifier,
                "head": self.head,
                "base": self.base,
                "state": "submitted",
                "schema_version": 1,
                "repository": "example/repo",
                "pr_number": 1,
            }
            if command == "review":
                item["phases"] = review()["phases"]
                item["phases"]["review"]["result"].update(
                    head=self.head, findings=self.findings
                )
            else:
                self.head = f"{self.number:040x}"
                child_id = f"{self.number:016x}"
                self.number += 1
                child = self.make_job(child_id, "review", self.head)
                child["job"].update(
                    expected_phases=["fixtures"], response_job=identifier
                )
                child["phases"] = {"fixtures": fixture()}
                item["phases"] = {
                    "response": {
                        "state": "completed",
                        "publication": "published",
                        "progress": {
                            "changed": True,
                            "push": "pushed",
                            "candidate": self.head,
                            "fixture_job": child_id,
                        },
                    }
                }
            self.receipts[action_id] = copy.deepcopy(item)
            result = item
        else:
            raise AssertionError(f"Unexpected command {command}")
        if command == self.crash:
            self.crash = None
            raise Crash()
        return copy.deepcopy(result)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, _ = driver.create(
            self.root, "central-example", self.root / "config.toml"
        )
        self.world = World()

    def tick(self):
        return driver.advance(self.path, self.world)

    def reach_review(self):
        self.assertEqual(self.tick()["stage"], "creation_wait")
        self.assertEqual(self.tick()["stage"], "review_submit")
        self.assertEqual(self.tick()["stage"], "review_wait")

    def test_clean_run_stops_without_merge_assess_or_evaluate(self):
        self.reach_review()
        result = self.tick()
        self.assertEqual(result["status"], "ready_for_merge", result)
        count = len(self.world.calls)
        self.tick()
        self.assertEqual(len(self.world.calls), count)
        self.assertEqual(
            {call[0] for call in self.world.calls},
            {"pr", "job", "status", "context", "review"},
        )

    def test_repair_then_clean_review(self):
        self.world.findings = [{"message": "Fix this"}]
        self.reach_review()
        result = self.tick()
        self.assertEqual((result["stage"], result["repairs"]), ("response_submit", 1))
        self.assertEqual(self.tick()["stage"], "response_wait")
        self.assertEqual(self.tick()["stage"], "review_submit")
        self.world.findings = []
        self.tick()
        result = self.tick()
        self.assertEqual(result["status"], "ready_for_merge", result)
        self.assertEqual(result["head"], self.world.head)
        self.assertEqual(len(self.world.receipts), 3)

    def test_five_repairs_cap(self):
        self.world.findings = [{"message": "Still broken"}]
        for _ in range(40):
            result = self.tick()
            if result["status"] != "running":
                break
        self.assertEqual(result.get("reason"), "repair_limit_reached", result)
        self.assertEqual(sum(call[0] == "respond" for call in self.world.calls), 5)
        self.assertEqual(sum(call[0] == "review" for call in self.world.calls), 6)

    def test_crash_after_creation_submission_reuses_creation(self):
        self.world.crash = "pr"
        with self.assertRaises(Crash):
            self.tick()
        self.assertEqual(driver.read(self.path)["stage"], "creation_submit")
        self.assertEqual(self.tick()["creation_job"], "1" * 16)

    def test_crash_after_review_and_response_submissions_reuses_receipts(self):
        self.tick()
        self.tick()
        self.world.findings = [{"message": "Fix"}]
        for command, stage in (("review", "review_wait"), ("respond", "response_wait")):
            self.world.crash = command
            with self.assertRaises(Crash):
                self.tick()
            before = len(self.world.receipts)
            self.assertEqual(self.tick()["stage"], stage)
            self.assertEqual(len(self.world.receipts), before)
            calls = [call for call in self.world.calls if call[0] == command]
            self.assertEqual(calls[-1], calls[-2])
            self.tick()

    def test_failed_missing_stale_and_unpublished_review_pause(self):
        self.reach_review()
        saved = driver.read(self.path)
        identifier = saved["active_job"]
        original = copy.deepcopy(self.world.jobs[identifier])
        mutations = [
            lambda item: item["phases"]["fixtures"].update(state="failed"),
            lambda item: item["phases"].pop("fixtures"),
            lambda item: item["phases"]["review"].update(publication="failed"),
            lambda item: item["phases"]["review"].update(result={}),
            lambda item: item["job"].update(head=OLD),
            lambda item: item["phases"]["fixtures"].update(
                worker_observation="unavailable"
            ),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                driver.write(self.path, saved)
                self.world.jobs[identifier] = copy.deepcopy(original)
                mutate(self.world.jobs[identifier])
                self.assertEqual(self.tick()["status"], "paused")
        self.assertFalse(any(call[0] == "respond" for call in self.world.calls))

    def test_pending_fixtures_wait_without_responding(self):
        self.reach_review()
        item = self.world.jobs[driver.read(self.path)["active_job"]]
        item["phases"]["fixtures"].update(state="running", publication="pending")
        result = self.tick()
        self.assertEqual(
            (result["status"], result["stage"]), ("running", "review_wait")
        )

    def fail_fixture(self, item, code=1):
        phase = item["phases"]["fixtures"]
        phase.update(state="failed")
        phase["process"]["exit_code"] = code

    def test_creation_validation_failure_routes_to_first_repair(self):
        self.tick()
        self.fail_fixture(self.world.jobs["f" * 16])
        state = self.tick()
        self.assertEqual((state["stage"], state["repairs"]), ("response_submit", 1))
        self.assertEqual(state["head"], HEAD)
        self.assertEqual(self.tick()["stage"], "response_wait")

    def test_failed_validation_overrides_clean_review_and_recovers(self):
        self.reach_review()
        self.fail_fixture(self.world.jobs[driver.read(self.path)["active_job"]], -6)
        state = self.tick()
        self.assertEqual((state["stage"], state["repairs"]), ("response_submit", 1))
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.tick()["status"], "ready_for_merge")

    def test_response_validation_failure_uses_new_head_and_action(self):
        self.world.findings = [{"message": "Fix"}]
        self.reach_review()
        self.tick()
        state = self.tick()
        child = self.world.jobs[state["active_job"]]["phases"]["response"]["progress"][
            "fixture_job"
        ]
        self.fail_fixture(self.world.jobs[child])
        candidate = self.world.head
        state = self.tick()
        self.assertEqual((state["stage"], state["repairs"]), ("response_submit", 2))
        self.assertEqual(state["head"], candidate)
        self.tick()
        calls = [call for call in self.world.calls if call[0] == "respond"]
        self.assertNotEqual(calls[0][3], calls[1][3])
        self.assertEqual(calls[1][5], candidate)

    def test_failed_fixture_waits_for_concurrent_review_then_repairs(self):
        self.reach_review()
        state = driver.read(self.path)
        item = self.world.jobs[state["active_job"]]
        self.fail_fixture(item)
        item["phases"]["review"].update(state="running", publication="pending")
        self.assertEqual(self.tick()["status"], "running")
        self.assertEqual(self.tick()["stage"], "review_wait")
        item["phases"]["review"].update(state="completed", publication="published")
        self.assertEqual(self.tick()["stage"], "response_submit")

    def test_validation_repairs_share_existing_limit(self):
        self.reach_review()
        state = driver.read(self.path)
        state["repairs"] = state["max_repairs"]
        driver.write(self.path, state)
        self.fail_fixture(self.world.jobs[state["active_job"]])
        self.assertEqual(self.tick()["reason"], "repair_limit_reached")
        self.assertFalse(any(call[0] == "respond" for call in self.world.calls))

    def test_failed_validation_does_not_bypass_other_boundaries(self):
        self.reach_review()
        saved = driver.read(self.path)
        identifier = saved["active_job"]
        self.fail_fixture(self.world.jobs[identifier])
        original = copy.deepcopy(self.world.jobs[identifier])
        mutations = [
            lambda i: i["phases"]["fixtures"].update(publication="pending"),
            lambda i: i["phases"]["fixtures"].update(candidate_unchanged=False),
            lambda i: i["phases"]["fixtures"].update(state="busy"),
            lambda i: i["phases"]["fixtures"]["process"].update(timed_out=True),
            lambda i: i["phases"]["fixtures"]["process"].update(interrupted=True),
            lambda i: i["phases"]["fixtures"]["process"].update(error="launch failed"),
            lambda i: i["phases"]["review"].update(result={}),
            lambda i: i["job"].update(head=OLD),
            lambda i: i["job"].update(base=OLD),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                driver.write(self.path, saved)
                self.world.jobs[identifier] = copy.deepcopy(original)
                mutate(self.world.jobs[identifier])
                self.assertEqual(self.tick()["status"], "paused")
        self.assertFalse(any(call[0] == "respond" for call in self.world.calls))

    def test_response_fixture_failure_pauses(self):
        self.world.findings = [{"message": "Fix"}]
        self.reach_review()
        self.tick()
        result = self.tick()
        child = self.world.jobs[result["active_job"]]["phases"]["response"]["progress"][
            "fixture_job"
        ]
        self.world.jobs[child]["phases"]["fixtures"].update(state="failed")
        self.assertEqual(self.tick()["status"], "paused")

    def test_resume_retains_stage_and_explicit_new_head_uses_new_action(self):
        self.reach_review()
        self.world.head = OLD
        paused = self.tick()
        self.assertEqual(paused["status"], "paused")
        resumed = driver.resume(self.path, commands=self.world)
        self.assertEqual(resumed["stage"], "review_wait")
        self.assertEqual(self.tick()["status"], "paused")
        resumed = driver.resume(
            self.path, review_current_head=True, commands=self.world
        )
        self.assertEqual((resumed["head"], resumed["generation"]), (OLD, 1))
        self.tick()
        self.assertEqual(self.tick()["status"], "ready_for_merge")
        self.assertEqual(len(self.world.receipts), 2)

    def test_existing_pr_without_creation_receipt_can_be_explicitly_adopted(self):
        result = driver.advance(
            self.path, lambda *args: {"bead_id": "central-example", "pr_url": URL}
        )
        self.assertEqual(result["reason"], "existing_pr_without_creation_receipt")
        self.assertEqual(result["pr_url"], URL)
        state = driver.resume(self.path, review_current_head=True, commands=self.world)
        self.assertEqual(state["stage"], "review_submit")
        self.tick()
        self.assertEqual(self.tick()["status"], "ready_for_merge")

    def test_creation_publication_failure_retains_url_for_operator_recovery(self):
        self.tick()
        self.world.creation["phases"]["creation"]["publication"] = "failed"
        result = self.tick()
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pr_url"], URL)
        state = driver.resume(self.path, review_current_head=True, commands=self.world)
        self.assertEqual(state["stage"], "review_submit")

    def test_base_change_pauses_before_review(self):
        self.tick()
        self.world.base = OLD
        self.assertEqual(self.tick()["status"], "paused")

    def test_duplicate_start_retains_state_and_excludes_other_driver(self):
        self.tick()
        path, created = driver.create(
            self.root, "central-example", self.root / "config.toml"
        )
        self.assertFalse(created)
        self.assertEqual(driver.read(path)["stage"], "creation_wait")
        with driver.lock(path):
            self.assertEqual(
                driver.create(self.root, "central-example", self.root / "config.toml"),
                (path, False),
            )
            for operation in (
                lambda: self.tick(),
                lambda: driver.resume(path, commands=self.world),
                lambda: cli.worker(path),
            ):
                with self.assertRaises(BlockingIOError):
                    operation()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_does_not_hold_an_inference_agent_while_waiting(self):
        self.world.creation["phases"]["creation"]["state"] = "running"

        def complete(_):
            self.world.creation["phases"]["creation"]["state"] = "completed"

        with (
            mock.patch.object(driver, "Commands", return_value=self.world),
            mock.patch.object(cli.time, "sleep", side_effect=complete) as sleep,
        ):
            cli.worker(self.path)
        sleep.assert_called_once_with(30)
        self.assertEqual(driver.read(self.path)["status"], "ready_for_merge")

    def test_invalid_receipt_pauses(self):
        self.tick()
        self.tick()

        def broken(*args):
            result = self.world(*args)
            result["action"]["head"] = OLD
            return result

        result = driver.advance(self.path, broken)
        self.assertEqual(result["reason"], "command_or_evidence_error")

    def test_observation_failure_retries_then_recovers_without_repair(self):
        self.reach_review()

        def failing(*args):
            raise driver.CommandFailure("status", "/private/failure.json")

        for count in (1, 2):
            result = driver.advance(self.path, failing)
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["observation_failures"], count)
            self.assertEqual(result["repairs"], 0)
        self.assertEqual(self.tick()["status"], "ready_for_merge")
        self.assertNotIn("observation_failures", driver.read(self.path))

    def test_creation_status_retry_limit_survives_successful_job_reads(self):
        self.tick()

        def failing(*args):
            if args[0] == "status":
                raise driver.CommandFailure("status")
            return self.world(*args)

        for _ in range(3):
            result = driver.advance(self.path, failing)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["observation_failures"], 3)
        self.assertEqual(result["stage"], "creation_wait")

    def test_uncertain_submission_does_not_use_observation_retries(self):
        def failing(*args):
            raise driver.CommandFailure("pr", "/private/pr.json")

        result = driver.advance(self.path, failing)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["details"]["diagnostic"], "/private/pr.json")

    def test_command_failure_retains_bounded_private_output(self):
        adapter = driver.Commands(self.root / "config.toml", self.root)
        process = subprocess.CompletedProcess(
            [], 1, "PRIVATE_SENTINEL" * 5000, "compiler unavailable"
        )
        with (
            mock.patch.object(driver.subprocess, "run", return_value=process),
            self.assertRaises(driver.CommandFailure) as caught,
        ):
            adapter("status", URL)
        path = Path(caught.exception.diagnostic)
        record = driver.read(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(record["stdout_tail"]), 32768)
        self.assertEqual(record["stderr_tail"], "compiler unavailable")
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_command_timeout_pauses_with_stage_retained(self):
        result = driver.advance(
            self.path, mock.Mock(side_effect=subprocess.TimeoutExpired("afk", 600))
        )
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["stage"], "creation_submit")


class CLITests(unittest.TestCase):
    def test_launcher_passes_executable_path_without_exporting_shell_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = driver.create(
                Path(temporary), "central-example", Path("/tmp/config")
            )
            with (
                mock.patch.dict(
                    cli.os.environ,
                    {"PATH": "/custom/bin:/usr/bin", "SECRET": "private"},
                    clear=True,
                ),
                mock.patch.object(cli.subprocess, "run") as run,
            ):
                cli.launch(path)
            argv = run.call_args.args[0]
            self.assertIn("--setenv=PATH=/custom/bin:/usr/bin", argv)
            self.assertFalse(
                any("SECRET" in part or "private" in part for part in argv)
            )

    def test_job_is_read_only_and_available_without_a_pr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = {"job": {"id": "1" * 16, "pr_url": None}, "phases": {}}
            with (
                mock.patch(
                    "afk_pr.config.load_config", return_value={"run_root": root}
                ),
                mock.patch("afk_pr.__main__.status_job", return_value=item) as probe,
                mock.patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(pr_main(["job", "1" * 16]), 0)
                self.assertEqual(json.loads(output.getvalue()), item)
                probe.assert_called_once_with(root / "pr-reviews" / ("1" * 16))

    def test_job_rejects_traversal_before_loading_config(self):
        with (
            mock.patch("afk_pr.config.load_config") as load,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(pr_main(["job", "../job"]), 1)
            load.assert_not_called()

    def test_launch_failure_keeps_recoverable_state_and_duplicate_start_does_not_launch(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "afk_pr.config.load_config", return_value={"run_root": root}
                ),
                mock.patch.object(
                    cli, "launch", side_effect=OSError("launch failed")
                ) as launch,
                mock.patch.object(
                    cli, "status", side_effect=lambda path: driver.read(path)
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(cli.main(["start", "central-example"]), 1)
                self.assertEqual(cli.main(["start", "central-example"]), 0)
                self.assertEqual(launch.call_count, 1)
                states = list(root.glob("orchestrations/*/state.json"))
                self.assertEqual(len(states), 1)
                self.assertEqual(driver.read(states[0])["stage"], "creation_submit")


class BoundaryTests(unittest.TestCase):
    def test_commands_adapter_uses_only_standalone_cli_json(self):
        adapter = driver.Commands(Path("/tmp/config.toml"))
        with mock.patch.object(
            driver.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, '{"ok": true}'),
        ) as call:
            self.assertEqual(adapter("job", "1" * 16), {"ok": True})
            self.assertEqual(
                call.call_args.args[0][1:],
                [
                    str(driver.ROOT / "afk"),
                    "job",
                    "1" * 16,
                    "--config",
                    "/tmp/config.toml",
                ],
            )
            adapter("context", URL)
            self.assertEqual(call.call_args.args[0][2:], ["context", URL])
        for output, code in (
            ("not json", 0),
            ("[]", 0),
            ('{"outcome":"failed"}', 0),
            ("{}", 1),
        ):
            with (
                mock.patch.object(
                    driver.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, output),
                ),
                self.assertRaises(RuntimeError),
            ):
                adapter("review", URL)

    def test_uncertain_creation_or_action_never_advances(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = driver.create(
                Path(temporary), "central-example", Path("/tmp/config")
            )
            state = driver.read(path)
            driver.step(state, lambda *args: {"pr_url": URL})
            self.assertEqual(state["reason"], "existing_pr_without_creation_receipt")
            state.update(
                status="running",
                stage="review_submit",
                pr_url=URL,
                head=HEAD,
                base=BASE,
            )
            driver.step(state, lambda *args: {"action": {"state": "paused"}})
            self.assertEqual(state["reason"], "submission_uncertain")
            self.assertEqual(state["stage"], "review_submit")

    def test_top_level_dispatches_optional_driver_and_independent_job(self):
        from afk_run import main

        with mock.patch.object(cli, "main", return_value=0) as run:
            self.assertEqual(main(["orchestrate", "status", "1" * 16]), 0)
            run.assert_called_once_with(["status", "1" * 16])
        with mock.patch("afk_pr.__main__.main", return_value=0) as run:
            self.assertEqual(main(["job", "1" * 16]), 0)
            run.assert_called_once_with(["job", "1" * 16])
