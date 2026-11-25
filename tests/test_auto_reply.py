import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import auto_reply
import draft_reply
import enable_live_autoreply


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
            "history_messages": 25,
            "memory_messages": 120,
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
        self.assertEqual(contacts[0]["tone"], "casual, natural and understated")
        self.assertEqual(history_limit, 15)
        self.assertEqual(memory_limit, 15)

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
    def test_reads_only_latest_fifteen_without_scrolling(self):
        visible = [
            {"id": str(index), "direction": "incoming", "text": f"message-{index}"}
            for index in range(20)
        ]
        driver = Mock()
        with patch.object(auto_reply, "extract_visible_messages", return_value=visible):
            result = auto_reply.read_recent_history(driver, 15)

        self.assertEqual([item["id"] for item in result], [str(i) for i in range(5, 20)])
        self.assertEqual(len(result), 15)
        driver.execute_script.assert_not_called()

    def test_respects_smaller_configured_limit(self):
        visible = [{"id": str(index), "text": str(index)} for index in range(10)]
        with patch.object(auto_reply, "extract_visible_messages", return_value=visible):
            result = auto_reply.read_recent_history(object(), 4)
        self.assertEqual([item["id"] for item in result], ["6", "7", "8", "9"])


class RecentMessageArrayTests(unittest.TestCase):
    def test_array_contains_up_to_fifteen_reference_messages_plus_latest(self):
        history = [
            {
                "speaker": "Contact" if index % 2 == 0 else "You",
                "direction": "incoming" if index % 2 == 0 else "outgoing",
                "text": f"history-{index}",
            }
            for index in range(20)
        ]
        messages = draft_reply._build_chat_messages(
            message="latest incoming",
            context="Relationship: friend",
            tone="warm",
            history=history,
        )

        conversation = messages[2:]
        self.assertEqual(len(conversation), 16)
        self.assertEqual(
            [item["content"] for item in conversation[:-1]],
            [f"history-{index}" for index in range(5, 20)],
        )
        self.assertEqual(conversation[-1], {
            "role": "user",
            "content": "Latest incoming WhatsApp message (reply to this only):" + "\n" + "latest incoming",
        })
        self.assertEqual(
            [item["role"] for item in conversation[:-1]],
            ["assistant", "user"] * 7 + ["assistant"],
        )
        self.assertIn("Roman Hindi/Hinglish", draft_reply.SYSTEM_PROMPT)
        self.assertIn("Do not add emojis by default", draft_reply.SYSTEM_PROMPT)
        self.assertIn("casual Roman Hindi/Hinglish", draft_reply.SYSTEM_PROMPT)
        self.assertIn("never flirt", draft_reply.SYSTEM_PROMPT)
        self.assertIn("Never use pet names", draft_reply.SYSTEM_PROMPT)
        self.assertIn("Never exceed 300 characters", draft_reply.SYSTEM_PROMPT)
        self.assertEqual(draft_reply.MAX_REPLY_CHARS, 300)

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


class StableMessageIdentityTests(unittest.TestCase):
    def test_timestamp_sender_metadata_survives_whatsapp_wrapper_rerender(self):
        driver = Mock()
        driver.execute_script.side_effect = [
            {"stableId": "wrapper-a", "metadata": "[02:01, 26/11/2026] Friend:"},
            {"stableId": "wrapper-b", "metadata": "[02:01, 26/11/2026] Friend:"},
        ]
        first = auto_reply._message_identity(driver, object(), "incoming", "Kya kr rhe ho")
        second = auto_reply._message_identity(driver, object(), "incoming", "Kya kr rhe ho")
        self.assertEqual(first, second)

    def test_message_identity_falls_back_to_whatsapp_stable_id(self):
        driver = Mock()
        driver.execute_script.return_value = {"stableId": "message-id-1", "metadata": ""}
        value = auto_reply._message_identity(driver, object(), "incoming", "Hi")
        self.assertIsInstance(value, str)
        self.assertEqual(len(value), 64)



class LatestPendingReplyTests(unittest.TestCase):
    def test_only_newest_pending_message_is_sent_for_drafting(self):
        history = [
            {"id": "base", "speaker": "Contact", "direction": "incoming", "text": "old"},
            {"id": "new-1", "speaker": "Contact", "direction": "incoming", "text": "Hi"},
            {"id": "new-2", "speaker": "Contact", "direction": "incoming", "text": "Kya kr rhe ho"},
            {"id": "new-3", "speaker": "Contact", "direction": "incoming", "text": "Pune me hu"},
        ]
        state = {
            "initialized": True,
            "last_processed_id": "base",
            "history": history[:1],
        }
        contact = {
            "phone_e164": "919876543210",
            "name": "Testing number",
            "relationship": "friend",
            "memory_notes": "",
            "tone": "warm and natural",
            "auto_send": False,
        }

        with patch.object(auto_reply, "make_draft", return_value="Acha, kab aayi?") as draft:
            with patch.object(auto_reply, "_write_state") as save:
                auto_reply._handle_new_messages(
                    driver=None,
                    contact=contact,
                    live=history,
                    state=state,
                    api_key="test-key",
                    memory_limit=15,
                    history_limit=15,
                    dry_run=True,
                )

        draft.assert_called_once()
        self.assertEqual(draft.call_args.kwargs["message"], "Pune me hu")
        self.assertEqual(
            [item["text"] for item in draft.call_args.kwargs["history"]],
            ["old", "Hi", "Kya kr rhe ho"],
        )
        self.assertEqual(state["last_processed_id"], "new-3")
        save.assert_called_once()


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


class LiveModeConfigTests(unittest.TestCase):
    def _write_config(self, directory: str, payload: dict) -> Path:
        path = Path(directory) / "contacts.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_enabling_live_updates_only_one_exact_enabled_contact_and_backs_up(self):
        payload = {
            "dry_run": True,
            "contacts": [
                {
                    "name": "Testing number",
                    "enabled": True,
                    "auto_send": False,
                    "tone": "warm and natural",
                },
                {"name": "Other", "enabled": False, "auto_send": False},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            backup = enable_live_autoreply.update_live_mode(
                path, "Testing number", enable_live=True
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            before = json.loads(backup.read_text(encoding="utf-8"))

        self.assertTrue(backup.is_file())
        self.assertTrue(before["dry_run"])
        self.assertFalse(saved["dry_run"])
        self.assertTrue(saved["contacts"][0]["auto_send"])
        self.assertEqual(saved["contacts"][0]["tone"], "casual, natural and understated")
        self.assertFalse(saved["contacts"][1]["auto_send"])

    def test_live_enable_refuses_more_than_one_enabled_contact_without_changes(self):
        payload = {
            "dry_run": True,
            "contacts": [
                {"name": "Testing number", "enabled": True, "auto_send": False},
                {"name": "Other", "enabled": True, "auto_send": False},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            with self.assertRaisesRegex(ValueError, "exactly one contact"):
                enable_live_autoreply.update_live_mode(
                    path, "Testing number", enable_live=True
                )
            after = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(after["dry_run"])
        self.assertFalse(after["contacts"][0]["auto_send"])

    def test_disable_live_restores_dry_run_for_every_contact(self):
        payload = {
            "dry_run": False,
            "contacts": [
                {"name": "A", "enabled": True, "auto_send": True},
                {"name": "B", "enabled": True, "auto_send": True},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory, payload)
            enable_live_autoreply.update_live_mode(path, None, enable_live=False)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(saved["dry_run"])
        self.assertTrue(all(not item["auto_send"] for item in saved["contacts"]))



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
    def _completion(text, finish_reason="stop", completion_tokens=25):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text, refusal=None),
                    finish_reason=finish_reason,
                )
            ],
            usage=SimpleNamespace(completion_tokens=completion_tokens),
        )

    def test_network_failure_retries_once_and_returns_reply(self):
        calls = {"count": 0}
        options = {}
        request_options = {}

        class FakeConnectionError(Exception):
            pass

        def fake_create(**kwargs):
            self.assertEqual(kwargs["model"], draft_reply.MODEL)
            request_options.update(kwargs)
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
        self.assertEqual(request_options["max_completion_tokens"], 1024)
        self.assertEqual(request_options["reasoning_effort"], "low")
        self.assertFalse(request_options["include_reasoning"])
        self.assertAlmostEqual(request_options["temperature"], 0.6)

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


    def test_blank_truncated_gpt_oss_completion_is_reported_without_retries(self):
        calls = {"count": 0}

        def fake_create(**_kwargs):
            calls["count"] += 1
            return self._completion("", finish_reason="length", completion_tokens=1024)

        with patch.object(draft_reply, "Groq", return_value=self.FakeClient(fake_create)):
            with self.assertRaises(draft_reply.GroqAPIError) as caught:
                draft_reply.make_draft("test-key", "Hello", "", "casual")

        self.assertFalse(caught.exception.retryable)
        self.assertIn("finish_reason=length", str(caught.exception))
        self.assertEqual(calls["count"], 1)

    def test_affectionate_draft_is_regenerated_before_being_returned(self):
        replies = iter([
            "Bas tumhare khayal mein hi hoon, jaan.",
            "Kuch nahi, tum batao?",
        ])
        request_messages = []

        def fake_create(**kwargs):
            request_messages.append([dict(item) for item in kwargs["messages"]])
            return self._completion(next(replies))

        with patch.object(draft_reply, "Groq", return_value=self.FakeClient(fake_create)):
            result = draft_reply.make_draft(
                "test-key", "Kya kr rhe ho?", "Relationship: friend", "casual"
            )

        self.assertEqual(result, "Kuch nahi, tum batao?")
        self.assertEqual(len(request_messages), 2)
        self.assertIn("STYLE VALIDATION FAILED", request_messages[1][-1]["content"])

    def test_empty_input_is_rejected_without_network(self):
        with patch.object(draft_reply, "Groq") as groq_client:
            with self.assertRaises(draft_reply.GroqAPIError) as caught:
                draft_reply.make_draft("test-key", "   ", "", "warm")
        self.assertFalse(caught.exception.retryable)
        groq_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
