import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from afk_pr.finish import FinishGitHub, finish
from afk_run import PreparationError, main

URL = "https://github.com/example/project/pull/1"


class FakeGitHub:
    def __init__(self):
        self.value = {
            "repository": "example/project",
            "head": "a" * 40,
            "base": "main",
            "merged": False,
            "state": "open",
            "merge_commit": None,
            "associations": ["parent", "other"],
            "observed_at": "now",
        }
        self.behavior = "merge"
        self.requests = []
        self.unreadable = False

    def observe(self, url):
        if self.unreadable:
            raise RuntimeError("cannot observe")
        return dict(self.value)

    def merge(self, intent, log):
        self.requests.append(dict(intent))
        if self.behavior == "race":
            self.value["head"] = "b" * 40
            raise RuntimeError("changed")
        if self.behavior == "deny":
            raise RuntimeError("denied")
        if self.behavior == "queue":
            return
        self.value.update(merged=True, state="closed", merge_commit="c" * 40)
        if self.behavior == "unreadable_after":
            self.unreadable = True
        if self.behavior == "lost_reply":
            raise subprocess.TimeoutExpired("gh", 120)


class FinishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(f'schema_version=1\nstate_root="{self.root}/state"\n')
        self.github = FakeGitHub()
        self.tasks = {"followup": {"id": "followup", "status": "open"}}
        self.closed = []
        self.closure_failure = False
        self.lost_close_reply = False

    def read(self, bead, config):
        if bead not in self.tasks:
            raise PreparationError("missing")
        return dict(self.tasks[bead])

    def close(self, bead, config, reason, log):
        self.closed.append(bead)
        if self.closure_failure:
            raise RuntimeError("failed")
        self.tasks[bead]["status"] = "closed"
        if self.lost_close_reply:
            raise RuntimeError("lost reply")

    def call(self, **kwargs):
        return finish(
            URL,
            self.config,
            github=self.github,
            read_bead=self.read,
            close_task=self.close,
            **kwargs,
        )

    def preview(self):
        return self.call(close_bead="followup")["id"]

    def test_preview_does_not_mutate_and_marker_is_only_hint(self):
        result = self.call()
        self.assertEqual(result["state"], "preview")
        self.assertIsNone(result["intent"]["close_bead"])
        self.assertEqual(result["observed"]["associations"], ["parent", "other"])
        applied = self.call(apply=result["id"])
        self.assertEqual(applied["closure"], "not_requested")
        self.assertFalse(self.closed)

    def test_success_and_restart_reconcile_without_repeating_mutations(self):
        preview = self.preview()
        self.assertFalse(self.github.requests)
        first = self.call(apply=preview)
        self.assertEqual(first["state"], "completed")
        second = self.call(apply=preview)
        self.assertEqual(second["closure"], "already_closed")
        self.assertEqual(len(self.github.requests), 1)
        self.assertEqual(self.closed, ["followup"])
        self.assertNotEqual(first["directory"], second["directory"])
        self.assertEqual(
            json.loads((Path(first["directory"]) / "result.json").read_text()), first
        )

    def test_changed_head_base_and_repository_stop_before_merge(self):
        preview = self.preview()
        for key in ("head", "base", "repository"):
            with self.subTest(key=key):
                old = self.github.value[key]
                self.github.value[key] = "changed"
                self.assertEqual(self.call(apply=preview)["merge"], "changed")
                self.github.value[key] = old
        self.assertFalse(self.github.requests)
        self.assertFalse(self.closed)

    def test_missing_bead_before_preview_or_execution_cannot_merge(self):
        with self.assertRaises(PreparationError):
            self.call(close_bead="missing")
        preview = self.preview()
        self.tasks.clear()
        result = self.call(apply=preview)
        self.assertEqual(result["error"], "PreparationError")
        self.assertFalse(self.github.requests)

    def test_race_denial_queue_and_closed_unmerged_do_not_close(self):
        preview = self.preview()
        for behavior, expected in (
            ("deny", "not_merged"),
            ("queue", "pending"),
            ("race", "changed"),
        ):
            self.github.behavior = behavior
            self.assertEqual(self.call(apply=preview)["merge"], expected)
        self.assertFalse(self.closed)
        self.github.value.update(head="a" * 40, state="closed")
        self.assertEqual(self.call(apply=preview)["merge"], "closed_unmerged")

    def test_queue_returns_and_later_observation_closes(self):
        preview = self.preview()
        self.github.behavior = "queue"
        self.assertEqual(self.call(apply=preview)["merge"], "pending")
        self.assertFalse(self.closed)
        self.github.value.update(merged=True, state="closed")
        self.assertEqual(self.call(apply=preview)["state"], "completed")
        self.assertEqual(len(self.github.requests), 1)

    def test_lost_merge_reply_and_unknown_observation_are_distinct(self):
        for behavior, expected in (
            ("lost_reply", "confirmed"),
            ("unreadable_after", "unknown"),
        ):
            self.github = FakeGitHub()
            self.tasks["followup"]["status"] = "open"
            self.closed.clear()
            preview = self.preview()
            self.github.behavior = behavior
            result = self.call(apply=preview)
            self.assertEqual(result["merge"], expected)
            if expected == "unknown":
                self.assertFalse(self.closed)
                self.github.unreadable = False
                self.assertEqual(self.call(apply=preview)["state"], "completed")
                self.assertEqual(len(self.github.requests), 1)

    def test_closure_failure_retry_and_lost_close_response(self):
        preview = self.preview()
        self.closure_failure = True
        result = self.call(apply=preview)
        self.assertEqual((result["merge"], result["closure"]), ("confirmed", "failed"))
        self.closure_failure = False
        self.lost_close_reply = True
        self.assertEqual(self.call(apply=preview)["closure"], "closed")
        self.assertEqual(len(self.github.requests), 1)

    def test_already_merged_and_unreadable_start(self):
        preview = self.preview()
        self.github.unreadable = True
        self.assertEqual(self.call(apply=preview)["merge"], "unknown")
        self.github.unreadable = False
        self.github.value.update(merged=True, state="closed")
        self.assertEqual(self.call(apply=preview)["closure"], "closed")
        self.assertFalse(self.github.requests)

    def test_execution_cannot_change_intent_or_select_other_pr(self):
        preview = self.preview()
        for kwargs in ({"close_bead": "other"}, {"method": "squash"}):
            with self.assertRaises(ValueError):
                self.call(apply=preview, **kwargs)
        with self.assertRaises(ValueError):
            finish(URL + "2", self.config, apply=preview, github=self.github)
        self.assertFalse(self.github.requests)

    def test_cli_dispatch_and_result_exit_codes(self):
        with patch(
            "afk_pr.finish.finish", return_value={"state": "preview"}
        ) as operation:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["finish", URL, "--close-bead", "followup"]), 0)
            self.assertEqual(operation.call_args.kwargs["close_bead"], "followup")
        with (
            patch("afk_pr.finish.finish", return_value={"state": "incomplete"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["finish", URL, "--apply", "a" * 16]), 1)

    def test_read_failure_after_confirmed_merge_retains_partial_result(self):
        preview = self.preview()
        calls = 0
        original = self.read

        def unreadable(bead, config):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PreparationError("tracker unavailable")
            return original(bead, config)

        result = finish(
            URL,
            self.config,
            apply=preview,
            github=self.github,
            read_bead=unreadable,
            close_task=self.close,
        )
        self.assertEqual((result["merge"], result["closure"]), ("confirmed", "unknown"))
        self.assertFalse(self.closed)
        self.assertEqual(self.call(apply=preview)["state"], "completed")
        self.assertEqual(len(self.github.requests), 1)

    def test_closure_rechecks_head_after_merge(self):
        preview = self.preview()
        original = self.github.observe
        calls = 0

        def moved(url):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.github.value["head"] = "b" * 40
            return original(url)

        self.github.observe = moved
        result = self.call(apply=preview)
        self.assertEqual(result["closure"], "not_confirmed")
        self.assertEqual(result["state"], "incomplete")
        self.assertFalse(self.closed)

    def test_same_preview_cannot_execute_concurrently(self):
        import fcntl

        preview = self.preview()
        with (self.root / "state" / "finishes" / preview / "apply.lock").open(
            "a"
        ) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already being applied"):
                self.call(apply=preview)
        self.assertFalse(self.github.requests)

    def test_native_observation_uses_actual_merge_and_association_hints(self):
        adapter = FinishGitHub()
        pr = {
            "head": {"sha": "a" * 40},
            "base": {"ref": "main", "repo": {"full_name": "Example/Project"}},
            "merged": False,
            "state": "closed",
            "merge_commit_sha": "test-merge-sha",
            "body": "<!-- afk-bead:parent --> <!-- afk-bead:followup -->",
        }
        with patch.object(adapter.github, "api", return_value=pr) as api:
            observed = adapter.observe(URL)
        api.assert_called_once_with("repos/example/project/pulls/1")
        self.assertFalse(observed["merged"])
        self.assertIsNone(observed["merge_commit"])
        self.assertEqual(observed["associations"], ["followup", "parent"])

    def test_native_command_has_head_match_without_bypass_or_deletion(self):
        adapter = FinishGitHub()
        intent = {"pr_url": URL, "method": "merge", "head": "a" * 40}
        with patch(
            "afk_pr.finish.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            adapter.merge(intent, self.root / "merge.log")
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "pr", "merge", URL, "--merge", "--match-head-commit", "a" * 40],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_tracker_credentials_scoped_and_closure_has_no_force(self):
        from afk_pr.beads import close_configured_bead

        secrets = self.root / "secrets"
        secrets.mkdir()
        (secrets / "dolt_beads_password.txt").write_text("test-only-password\n")
        with patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            close_configured_bead(
                "followup",
                {"beads_workspace": str(self.root)},
                "merged PR",
                self.root / "beads.log",
            )
        self.assertEqual(
            run.call_args.args[0], ["bd", "close", "followup", "--reason", "merged PR"]
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["BEADS_DOLT_PASSWORD"], "test-only-password"
        )


if __name__ == "__main__":
    unittest.main()
