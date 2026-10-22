"""Exercise cooperative cleanup through the queued PR fixture worker."""

import sys
from unittest import mock

from afk_pr import jobs


def seal_fixture_result(directory):
    """Stand in for a trusted terminal worker in publication-only test fixtures."""
    from afk_pr.validation import publication_seal, seal

    job = jobs.read(directory / "job.json")
    record = jobs.read(directory / "fixtures.json")
    jobs.write(
        directory / "fixtures-seal.json", {"sha256": seal(directory, job, record)}
    )
    if record.get("publication") == "published":
        jobs.write(
            directory / "fixtures-publication-seal.json",
            {"sha256": publication_seal(record)},
        )


def slow_cleanup_policy():
    script = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, lambda *_: "
        "(time.sleep(2.1),print('cleanup complete',flush=True),exit(0))); "
        "print('fixture started',flush=True); time.sleep(60)"
    )
    return {
        "command": [sys.executable, "-c", script],
        "timeout_seconds": 1,
        "termination_grace_seconds": 4,
        "repairable_exit_codes": [1],
    }


def assert_slow_cleanup(test, directory, github):
    """The process exceeds both its command bound and runtime's default grace."""
    with (
        mock.patch.object(jobs, "GitHub", return_value=github),
        mock.patch.object(jobs, "run_command", wraps=jobs.run_command) as run,
    ):
        jobs.worker(directory, "fixtures")
    record = jobs.read(directory / "fixtures.json")
    test.assertEqual(run.call_args.kwargs["termination_grace_seconds"], 4)
    test.assertEqual(record["state"], "timed_out")
    test.assertEqual(record["publication"], "published")
    test.assertEqual(record["process"]["exit_code"], 0)
    test.assertTrue(record["process"]["timed_out"])
    test.assertFalse(record["process"]["interrupted"])
    test.assertTrue(record["candidate_unchanged"])
    test.assertIn("cleanup complete", (directory / "fixtures.stdout.log").read_text())
