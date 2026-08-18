import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import afk_runtime


class ResistantProcess:
    pid = 4321
    returncode = None

    def __init__(self):
        self.wait_timeouts = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(["resistant-child"], timeout)


class RunCommandTerminationTest(unittest.TestCase):
    def test_post_sigkill_reap_timeout_is_bounded_and_reported(self):
        process = ResistantProcess()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("afk_runtime.subprocess.Popen", return_value=process),
                mock.patch("afk_runtime.os.killpg") as killpg,
            ):
                result = afk_runtime.run_command(
                    ["resistant-child"],
                    root,
                    7,
                    root / "stdout.log",
                    root / "stderr.log",
                )

        self.assertEqual(
            process.wait_timeouts,
            [
                7,
                afk_runtime.TERMINATION_GRACE_SECONDS,
                afk_runtime.REAP_GRACE_SECONDS,
            ],
        )
        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [afk_runtime.signal.SIGTERM, afk_runtime.signal.SIGKILL],
        )
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["interrupted"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("SIGKILL", result["error"])
        self.assertIn("2 seconds", result["error"])


if __name__ == "__main__":
    unittest.main()
