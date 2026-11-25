import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import auto_reply
import draft_reply


class PhoneNormalizationTests(unittest.TestCase):
    def test_local_and_international_numbers(self):
        self.assertEqual(auto_reply.normalize_phone("98765 43210", "91"), "919876543210")
        self.assertEqual(auto_reply.normalize_phone("+91 98765-43210", "91"), "919876543210")
        self.assertEqual(auto_reply.normalize_phone("919876543210", "91"), "919876543210")

    def test_rejects_invalid_numbers(self):
        for value in ("", "12345", "abcdefghij"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    auto_reply.normalize_phone(value, "91")


class ConfigTests(unittest.TestCase):
    def _write_config(self, directory: str, payload: dict) -> Path:
        path = Path(directory) / "contacts.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_dry_run_defaults_to_true_and_contact_is_disabled_by_default(self):
        payload = {
            "contacts": [
                {
                    "phone": "9876543210",
                    "name": "Test contact",
                    "whatsapp_name": "Exact WhatsApp title",
                    "enabled": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            with patch.object(auto_reply, "CONFIG_FILE", path):
                contacts, history_limit, memory_limit, dry_run = auto_reply.load_config()

        self.assertTrue(dry_run)
        self.assertEqual(len(contacts), 1)
        self.assertFalse(contacts[0]["auto_send"])
        self.assertEqual(contacts[0]["whatsapp_name"], "Exact WhatsApp title")
        self.assertEqual(history_limit, 10)
        self.assertEqual(memory_limit, 10)

    def test_string_boolean_is_rejected(self):
        payload = {
            "dry_run": False,
            "contacts": [
                {
                    "phone": "9876543210",
                    "name": "Test contact",
                    "enabled": "true",
                    "auto_send": "false",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            with patch.object(auto_reply, "CONFIG_FILE", path):
                with self.assertRaisesRegex(ValueError, "JSON boolean"):
                    auto_reply.load_config()

    def test_duplicate_enabled_numbers_are_rejected(self):
        payload = {
            "dry_run": True,
            "contacts": [
                {"phone": "9876543210", "name": "A", "enabled": True},
                {"phone": "+919876543210", "name": "B", "enabled": True},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            with patch.object(auto_reply, "CONFIG_FILE", path):
                with self.assertRaisesRegex(ValueError, "Duplicate"):
                    auto_reply.load_config()


class RecentHistoryTests(unittest.TestCase):
    def test_reads_only_latest_ten_without_scrolling(self):
        visible = [
            {"id": str(index), "direction": "incoming", "text": f"message-{index}"}
            for index in range(15)
        ]
        with patch.object(auto_reply, "extract_visible_messages", return_value=visible):
            with patch.object(
                auto_reply,
                "_find_message_scroller",
                side_effect=AssertionError("history reader must not scroll"),
            ) as scroll:
                result = auto_reply.read_recent_history(object(), 10)

        self.assertEqual([item["id"] for item in result], [str(i) for i in range(5, 15)])
        self.assertEqual(len(result), 10)
        scroll.assert_not_called()

    def test_respects_smaller_configured_limit(self):
        visible = [{"id": str(index), "text": str(index)} for index in range(10)]
        with patch.object(auto_reply, "extract_visible_messages", return_value=visible):
            result = auto_reply.read_recent_history(object(), 4)
        self.assertEqual([item["id"] for item in result], ["6", "7", "8", "9"])


class RecentMessageArrayTests(unittest.TestCase):
    def test_array_contains_only_nine_prior_messages_plus_latest(self):
        history = [
            {
                "speaker": "Contact" if index % 2 == 0 else "You",
                "direction": "incoming" if index % 2 == 0 else "outgoing",
                "text": f"history-{index}",
            }
            for index in range(15)
        ]
        messages = draft_reply._build_chat_messages(
            message="latest incoming",
            context="Relationship: friend",
            tone="warm",
            history=history,
        )

        conversation = messages[2:]
        self.assertEqual(len(conversation), 10)
        self.assertEqual(
            [item["content"] for item in conversation[:-1]],
            [f"history-{index}" for index in range(6, 15)],
        )
        self.assertEqual(conversation[-1], {
            "role": "user",
            "content": "Latest incoming WhatsApp message (reply to this only):\\nlatest incoming",
        })
        self.assertEqual(
            [item["role"] for item in conversation[:-1]],
            ["user", "assistant"] * 4 + ["user"],
        )

    def test_empty_history_still_sends_latest_message(self):
        messages = draft_reply._build_chat_messages(
            message="hello", context="", tone="warm", history=[]
        )
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[-1]["role"], "user")


class DirectionClassificationTests(unittest.TestCase):
    def test_explicit_tail_markers_take_precedence(self):
        self.assertEqual(
            auto_reply._classify_message_direction(True, False, None, None, None, None),
            "incoming",
        )
        self.assertEqual(
            auto_reply._classify_message_direction(False, True, None, None, None, None),
            "outgoing",
        )

    def test_conflicting_markers_are_not_guessed(self):
        self.assertEqual(
            auto_reply._classify_message_direction(True, True, 10, 20, 0, 100),
            "unknown",
        )

    def test_left_and_right_alignment_fallback(self):
        self.assertEqual(
            auto_reply._classify_message_direction(False, False, 100, 220, 0, 1000),
            "incoming",
        )
        self.assertEqual(
            auto_reply._classify_message_direction(False, False, 780, 900, 0, 1000),
            "outgoing",
        )

    def test_center_dead_zone_and_missing_geometry_are_unknown(self):
        self.assertEqual(
            auto_reply._classify_message_direction(False, False, 450, 550, 0, 1000),
            "unknown",
        )
        self.assertEqual(
            auto_reply._classify_message_direction(False, False, None, None, 0, 1000),
            "unknown",
        )


class MessageStateTests(unittest.TestCase):
    def test_pending_messages_are_only_incoming_after_saved_marker(self):
        history = [
            {"id": "old-in", "direction": "incoming", "text": "old"},
            {"id": "old-out", "direction": "outgoing", "text": "reply"},
            {"id": "marker", "direction": "incoming", "text": "already done"},
            {"id": "new-out", "direction": "outgoing", "text": "another reply"},
            {"id": "new-in", "direction": "incoming", "text": "new message"},
        ]
        state = {"last_processed_id": "marker"}
        self.assertEqual(
            [m["id"] for m in auto_reply._pending_messages(history, state)],
            ["new-in"],
        )

    def test_missing_marker_is_skipped_once_then_baseline_recovers(self):
        history = [
            {"id": "newest", "direction": "incoming", "text": "arrived after long idle"},
        ]
        state = {"last_processed_id": "older-marker-no-longer-visible"}
        self.assertEqual(auto_reply._pending_messages(history, state), [])
        self.assertEqual(state["last_processed_id"], "newest")

    def test_memory_merge_keeps_previously_saved_context(self):
        saved = [
            {"id": "a", "direction": "incoming", "text": "one"},
            {"id": "b", "direction": "outgoing", "text": "two"},
        ]
        live = [
            {"id": "b", "direction": "outgoing", "text": "two"},
            {"id": "c", "direction": "incoming", "text": "three"},
        ]
        merged = auto_reply._merge_memory(saved, live, 20)
        self.assertEqual([m["id"] for m in merged], ["a", "b", "c"])


class GroqRetryTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, create):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    @staticmethod
    def _completion(text):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )

    def test_network_failure_retries_once_and_returns_reply(self):
        calls = {"count": 0}
        options = {}

        class FakeConnectionError(Exception):
            pass

        def fake_create(**kwargs):
            self.assertEqual(kwargs["model"], draft_reply.MODEL)
            self.assertEqual(kwargs["max_completion_tokens"], 180)
            calls["count"] += 1
            if calls["count"] == 1:
                raise FakeConnectionError("temporary network problem")
            return self._completion("Haan, bilkul. Main dekh leti hoon.")

        def fake_groq(**kwargs):
            options.update(kwargs)
            return self.FakeClient(fake_create)

        with patch.object(draft_reply, "Groq", side_effect=fake_groq):
            with patch.object(draft_reply, "APIConnectionError", FakeConnectionError):
                with patch.object(draft_reply.time, "sleep"):
                    result = draft_reply.make_draft(
                        "test-key",
                        "Can you check this?",
                        "Relationship: friend",
                        "warm",
                    )

        self.assertEqual(result, "Haan, bilkul. Main dekh leti hoon.")
        self.assertEqual(calls["count"], 2)
        self.assertEqual(options["max_retries"], 0)
        self.assertEqual(options["timeout"], draft_reply.REQUEST_TIMEOUT_SECONDS)

    def test_cloudflare_403_is_not_retried(self):
        calls = {"count": 0}

        class FakeResponse:
            text = json.dumps({
                "error_code": 1010,
                "error_name": "browser_signature_banned",
                "detail": "Access denied",
            })
            headers = {}

        class FakeStatusError(Exception):
            status_code = 403
            response = FakeResponse()

        def fake_create(**_kwargs):
            calls["count"] += 1
            raise FakeStatusError("403 access denied")

        with patch.object(draft_reply, "Groq", return_value=self.FakeClient(fake_create)):
            with patch.object(draft_reply, "APIStatusError", FakeStatusError):
                with self.assertRaises(draft_reply.GroqAPIError) as caught:
                    draft_reply.make_draft(
                        "test-key", "Hello", "Relationship: friend", "warm"
                    )

        self.assertEqual(caught.exception.status_code, 403)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls["count"], 1)

    def test_empty_input_is_rejected_without_network(self):
        with patch.object(draft_reply, "Groq") as groq_client:
            with self.assertRaises(draft_reply.GroqAPIError) as caught:
                draft_reply.make_draft("test-key", "   ", "", "warm")
        self.assertFalse(caught.exception.retryable)
        groq_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
