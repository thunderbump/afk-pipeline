import json
import tempfile
import unittest
from pathlib import Path

from afk_agent import MAX_AUTO_RETRIES, agent_response
from afk_export import event_counts


class AgentResponseRetryTest(unittest.TestCase):
    def interpret(self, events):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            return agent_response(path)

    @staticmethod
    def message(stop_reason, text="", error=None):
        message = {
            "role": "assistant",
            "stopReason": stop_reason,
            "content": [{"type": "text", "text": text}],
        }
        if error is not None:
            message["errorMessage"] = error
        return {"type": "message_end", "message": message}

    @staticmethod
    def tool_use():
        return {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "synthetic-call",
                        "name": "read",
                        "arguments": {"path": "synthetic.txt"},
                    }
                ],
            },
        }

    @staticmethod
    def retry_start(attempt):
        return {
            "type": "auto_retry_start",
            "attempt": attempt,
            "maxAttempts": MAX_AUTO_RETRIES,
            "delayMs": 100 * attempt,
            "errorMessage": "temporary model failure",
        }

    def successful_retry(self):
        # This preserves Pi's real event ordering while using synthetic content.
        return [
            {"type": "agent_start"},
            self.message("error", "discarded", "temporary model failure"),
            {"type": "turn_end"},
            {"type": "agent_end", "willRetry": True},
            self.retry_start(1),
            {"type": "agent_start"},
            {"type": "turn_start"},
            self.tool_use(),
            {"type": "auto_retry_end", "success": True, "attempt": 1},
            {"type": "turn_end"},
            {"type": "message_end", "message": {"role": "toolResult"}},
            self.message("stop", "final answer"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]

    def test_accepts_pi_auto_retry_and_uses_only_final_message(self):
        self.assertEqual(
            self.interpret(self.successful_retry()),
            {"agent": {"status": "completed"}, "text": "final answer"},
        )

    def test_accepts_omitted_will_retry_on_final_retry_terminal(self):
        events = self.successful_retry()
        final_end = next(
            event
            for event in events
            if event.get("type") == "agent_end" and event.get("willRetry") is False
        )
        del final_end["willRetry"]

        self.assertEqual(
            self.interpret(events),
            {"agent": {"status": "completed"}, "text": "final answer"},
        )

    def test_accepts_multiple_bounded_retry_segments(self):
        events = [
            {"type": "agent_start"},
            self.message("error", "discarded", "temporary model failure"),
            {"type": "agent_end", "willRetry": True},
            self.retry_start(1),
            {"type": "agent_start"},
            self.message("error", "also discarded", "temporary model failure"),
            {"type": "agent_end", "willRetry": True},
            self.retry_start(2),
            {"type": "agent_start"},
            self.message("stop", "final answer"),
            {"type": "auto_retry_end", "success": True, "attempt": 2},
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
        self.assertEqual(self.interpret(events)["agent"], {"status": "completed"})

    def test_retry_ordering_and_final_settlement_fail_closed(self):
        valid = self.successful_retry()
        variants = {
            "retry without declaration": valid[:3] + [valid[4]] + valid[5:],
            "event before retry start": valid[:4]
            + [{"type": "turn_start"}]
            + valid[4:],
            "missing new segment": valid[:5] + valid[6:],
            "missing retry completion": [
                event for event in valid if event.get("type") != "auto_retry_end"
            ],
            "retry completes before successful response": [
                event
                for event in valid
                if event.get("message", {}).get("stopReason") != "toolUse"
            ],
            "missing final settlement": valid[:-1],
            "final terminal retries again": [
                {**event, "willRetry": True}
                if event.get("type") == "agent_end" and event.get("willRetry") is False
                else event
                for event in valid
            ],
            "events after final end": valid + [{"type": "queue_update"}],
        }
        for name, events in variants.items():
            with self.subTest(name=name):
                self.assertEqual(self.interpret(events)["agent"]["status"], "error")

    def test_malformed_retry_metadata_fails_closed(self):
        for field, value in (
            ("attempt", True),
            ("maxAttempts", False),
            ("delayMs", "soon"),
            ("errorMessage", None),
        ):
            with self.subTest(field=field):
                events = self.successful_retry()
                retry = next(
                    event for event in events if event.get("type") == "auto_retry_start"
                )
                retry[field] = value
                self.assertEqual(self.interpret(events)["agent"]["status"], "error")

    def test_conflicting_terminal_messages_fail_closed(self):
        for following_type in ("auto_retry_end", "agent_end"):
            with self.subTest(following_type=following_type):
                events = self.successful_retry()
                insertion = next(
                    index
                    for index, event in enumerate(events)
                    if event.get("type") == following_type
                    and (
                        following_type != "agent_end" or event.get("willRetry") is False
                    )
                )
                events.insert(insertion, self.message("stop", "conflicting answer"))
                self.assertEqual(self.interpret(events)["agent"]["status"], "error")

    def test_export_summary_recognizes_retry_events(self):
        counts = event_counts(
            "\n".join(json.dumps(event) for event in self.successful_retry())
        )
        self.assertEqual(counts["auto_retry_start"], 1)
        self.assertEqual(counts["auto_retry_end"], 1)
        self.assertEqual(counts["unknown"], 0)

    def test_rejects_retry_loop_beyond_bound(self):
        events = [{"type": "agent_start"}]
        for attempt in range(1, MAX_AUTO_RETRIES + 2):
            events.extend(
                [
                    self.message("error", "discarded", "temporary model failure"),
                    {"type": "agent_end", "willRetry": True},
                    self.retry_start(attempt),
                    {"type": "agent_start"},
                ]
            )
        self.assertEqual(self.interpret(events)["agent"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
