import json
import unittest

from afk_attempt.transcript import build_attempt_transcript
from afk_export import REDACTED_SECRET, sanitize_public_artifact_text


def event(**value):
    return json.dumps(value, separators=(",", ":"))


def stream(*events):
    return ("\n".join(events) + "\n").encode()


def sanitizer(value):
    return sanitize_public_artifact_text(value, {"/home/operator/work"})


class AttemptTranscriptTest(unittest.TestCase):
    def complete(self, *middle):
        return stream(
            event(type="agent_start"),
            *middle,
            event(
                type="message_end",
                message={
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "private final response"}],
                },
            ),
            event(type="agent_end"),
        )

    def test_commands_and_file_edits_keep_only_allowlisted_facts(self):
        raw = self.complete(
            event(
                type="tool_execution_start",
                toolCallId="one",
                toolName="bash",
                args={
                    "command": "cat /home/operator/work/a.txt",
                    "description": "secret prompt",
                },
            ),
            event(
                type="tool_execution_start",
                toolCallId="two",
                toolName="edit",
                args={
                    "path": "/home/operator/work/code.py",
                    "oldText": "password=do-not-publish",
                    "newText": "file contents do not publish",
                },
            ),
            event(
                type="tool_execution_end",
                toolCallId="two",
                toolName="edit",
                result={
                    "content": [{"type": "text", "text": "whole file"}],
                    "isError": False,
                },
            ),
        )

        transcript = build_attempt_transcript(raw, sanitizer)

        command = next(
            item for item in transcript["records"] if item.get("tool") == "bash"
        )
        edit = next(
            item for item in transcript["records"] if item.get("tool") == "edit"
        )
        self.assertTrue(command["command"].startswith("cat [redacted-path]"))
        self.assertNotIn("/home/operator", command["command"])
        self.assertNotIn("/home/operator", edit["path"])
        self.assertEqual(edit["operation"], "replace")
        self.assertEqual(edit["omitted_fields"], ["oldText", "newText"])
        rendered = json.dumps(transcript)
        for private in (
            "secret prompt",
            "do-not-publish",
            "file contents",
            "whole file",
            "private final",
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            [item["sequence"] for item in transcript["records"]],
            sorted(item["sequence"] for item in transcript["records"]),
        )

    def test_provider_retries_are_ordered_and_error_is_sanitized(self):
        raw = stream(
            event(type="agent_start"),
            event(
                type="message_end",
                message={
                    "role": "assistant",
                    "stopReason": "error",
                    "content": [],
                    "errorMessage": "failed",
                },
            ),
            event(type="agent_end", willRetry=True),
            event(
                type="auto_retry_start",
                attempt=1,
                maxAttempts=3,
                delayMs=25,
                errorMessage="token=abcdefghijklmnop /home/operator/work/key",
            ),
            event(type="agent_start"),
            event(
                type="message_end",
                message={"role": "assistant", "stopReason": "stop", "content": []},
            ),
            event(type="auto_retry_end", attempt=1, success=True),
            event(type="agent_end"),
            event(type="agent_settled"),
        )
        transcript = build_attempt_transcript(raw, sanitizer)
        retry = next(
            item
            for item in transcript["records"]
            if item["event"] == "provider_retry_started"
        )
        self.assertTrue(retry["error"].startswith(f"{REDACTED_SECRET} [redacted-path]"))
        self.assertNotIn("/home/operator", retry["error"])
        self.assertEqual(retry["attempt"], 1)

    def test_malformed_events_and_private_keys_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "validated Attempt event stream"):
            build_attempt_transcript(b'{"type":\n', sanitizer)
        raw = self.complete(
            event(
                type="tool_execution_start",
                toolName="bash",
                args={"command": "echo -----BEGIN PRIVATE KEY-----"},
            )
        )
        with self.assertRaisesRegex(ValueError, "unsafe transcript content"):
            build_attempt_transcript(raw, sanitizer)

    def test_unknown_events_are_omitted_and_counted(self):
        raw = self.complete(event(type="future_event", prompt="never publish me"))
        transcript = build_attempt_transcript(raw, sanitizer)
        self.assertNotIn("never publish me", json.dumps(transcript))
        self.assertEqual(
            transcript["omissions"],
            [
                {
                    "reason": "event_not_allowlisted",
                    "event_type": "future_event",
                    "count": 1,
                }
            ],
        )

    def test_empty_session_is_explicit(self):
        transcript = build_attempt_transcript(b"", sanitizer)
        self.assertEqual(transcript["status"], "empty")
        self.assertEqual(
            transcript["records"], [{"sequence": 0, "event": "empty_session"}]
        )

    def test_size_limit_adds_an_explicit_truncation_record(self):
        middle = [
            event(
                type="tool_execution_start",
                toolName="bash",
                args={"command": "echo " + ("x" * 200)},
            )
            for _ in range(20)
        ]
        transcript = build_attempt_transcript(
            self.complete(*middle), sanitizer, max_bytes=1200
        )
        self.assertEqual(transcript["status"], "truncated")
        self.assertEqual(transcript["records"][-1]["event"], "transcript_truncated")
        self.assertGreater(transcript["records"][-1]["omitted_records"], 0)
        self.assertLessEqual(
            len(json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode()),
            1200,
        )


if __name__ == "__main__":
    unittest.main()
