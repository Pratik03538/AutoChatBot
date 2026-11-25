import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

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
        self.assertGreaterEqual(history_limit, 5)
        self.assertGreaterEqual(memory_limit, 20)

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
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "Haan, bilkul. Main dekh leti hoon."}}]
            }).encode("utf-8")

    def test_network_failure_retries_once_and_returns_reply(self):
        calls = {"count": 0}

        def fake_urlopen(_request, timeout):
            self.assertEqual(timeout, draft_reply.REQUEST_TIMEOUT_SECONDS)
            calls["count"] += 1
            if calls["count"] == 1:
                raise URLError("temporary network problem")
            return self.FakeResponse()

        with patch.object(draft_reply, "urlopen", side_effect=fake_urlopen):
            with patch.object(draft_reply.time, "sleep"):
                result = draft_reply.make_draft(
                    "test-key",
                    "Can you check this?",
                    "Relationship: friend",
                    "warm",
                )

        self.assertEqual(result, "Haan, bilkul. Main dekh leti hoon.")
        self.assertEqual(calls["count"], 2)

    def test_empty_input_is_rejected_without_network(self):
        with self.assertRaises(draft_reply.GroqAPIError) as caught:
            draft_reply.make_draft("test-key", "   ", "", "warm")
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
