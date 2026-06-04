"""
Integration tests for the Telegram handler.

All tests use FakeMessage / FakeUpdate to avoid any real Telegram network calls.
DEMO_MODE=true so no Anthropic calls are made.
All DB calls are awaited — the DB layer is now fully async (aiosqlite).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-123")
os.environ.setdefault("DEMO_MODE", "true")

import conversation
import db
from config import settings
from telegram_handler import (
    _authorized_chat,
    _message_text,
    _triggered_for_group,
    _within_rate_limit,
    handle_message,
    cancel_command,
    reset_command,
    status_command,
)
import telegram_handler as th


# ---------------------------------------------------------------------------
# Fake Telegram objects
# ---------------------------------------------------------------------------

class FakeFile:
    async def download_to_drive(self, custom_path: str) -> None:
        pass


class FakePhotoSize:
    async def get_file(self) -> FakeFile:
        return FakeFile()


class FakeBot:
    async def send_chat_action(self, **kwargs) -> None:
        pass  # swallow typing indicators


class FakeMessage:
    def __init__(self, message_id: int, text: str | None = None, caption: str | None = None) -> None:
        self.message_id = message_id
        self.chat_id = -123
        self.message_thread_id = None
        self.from_user = SimpleNamespace(id=42)
        self.text = text
        self.caption = caption
        self.reply_to_message = None
        self.replies: list[str] = []
        self.photo: list = []
        self._bot = FakeBot()

    def get_bot(self):
        return self._bot

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, update_id: int, message: FakeMessage) -> None:
        self.update_id = update_id
        self.message = message


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------

class MessageTextTests(unittest.TestCase):
    def test_text_message(self) -> None:
        msg = FakeMessage(1, text="  hello  ")
        self.assertEqual(_message_text(msg), "hello")

    def test_caption_message(self) -> None:
        msg = FakeMessage(1, caption="  caption  ")
        self.assertEqual(_message_text(msg), "caption")

    def test_none_when_both_missing(self) -> None:
        msg = FakeMessage(1)
        self.assertIsNone(_message_text(msg))

    def test_none_for_whitespace_only(self) -> None:
        msg = FakeMessage(1, text="   ")
        self.assertIsNone(_message_text(msg))


class AuthorizationTests(unittest.TestCase):
    def test_authorized_for_configured_chat(self) -> None:
        msg = FakeMessage(1)
        msg.chat_id = settings.TELEGRAM_CHAT_ID
        self.assertTrue(_authorized_chat(msg))

    def test_unauthorized_for_other_chat(self) -> None:
        msg = FakeMessage(1)
        msg.chat_id = 99999
        self.assertFalse(_authorized_chat(msg))


class TriggerModeTests(unittest.TestCase):
    def tearDown(self) -> None:
        settings.GROUP_TRIGGER_MODE = "all"
        settings.BOT_USERNAME = ""

    def test_all_mode_always_triggers(self) -> None:
        settings.GROUP_TRIGGER_MODE = "all"
        self.assertTrue(_triggered_for_group(FakeMessage(1), "hello"))

    def test_reply_or_mention_triggers_on_reply(self) -> None:
        settings.GROUP_TRIGGER_MODE = "reply_or_mention"
        msg = FakeMessage(1, text="hello")
        msg.reply_to_message = SimpleNamespace(message_id=99)
        self.assertTrue(_triggered_for_group(msg, "hello"))

    def test_reply_or_mention_triggers_on_mention(self) -> None:
        settings.GROUP_TRIGGER_MODE = "reply_or_mention"
        settings.BOT_USERNAME = "testbot"
        self.assertTrue(_triggered_for_group(FakeMessage(1), "hey @testbot what do you think?"))

    def test_reply_or_mention_does_not_trigger_plain_message(self) -> None:
        settings.GROUP_TRIGGER_MODE = "reply_or_mention"
        settings.BOT_USERNAME = "testbot"
        self.assertFalse(_triggered_for_group(FakeMessage(1), "just chatting"))


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        # Clear rate window for a fresh test user
        th._rate_window[9999] = []

    def test_within_limit(self) -> None:
        settings.RATE_LIMIT_MESSAGES = 5
        settings.RATE_LIMIT_WINDOW_SECONDS = 60
        for _ in range(5):
            self.assertTrue(_within_rate_limit(9999))

    def test_exceeds_limit(self) -> None:
        settings.RATE_LIMIT_MESSAGES = 3
        settings.RATE_LIMIT_WINDOW_SECONDS = 60
        th._rate_window[9999] = []
        for _ in range(3):
            _within_rate_limit(9999)
        self.assertFalse(_within_rate_limit(9999))

    def test_disabled_when_zero(self) -> None:
        settings.RATE_LIMIT_MESSAGES = 0
        for _ in range(100):
            self.assertTrue(_within_rate_limit(9999))

    def tearDown(self) -> None:
        settings.RATE_LIMIT_MESSAGES = 10
        settings.RATE_LIMIT_WINDOW_SECONDS = 60
        th._rate_window.clear()


# ---------------------------------------------------------------------------
# End-to-end async flow tests
# ---------------------------------------------------------------------------

class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings.DATABASE_PATH = str(Path(self.tmp.name) / "test.db")
        settings.TELEGRAM_CHAT_ID = -123
        settings.DEMO_MODE = True
        settings.GROUP_TRIGGER_MODE = "all"
        settings.RATE_LIMIT_MESSAGES = 0  # disable rate limiting during tests
        th._rate_window.clear()
        conversation.clear_all()
        await db.init_db()

    async def asyncTearDown(self) -> None:
        conversation.clear_all()
        th._rate_window.clear()
        self.tmp.cleanup()

    async def test_vague_listing_asks_for_clarification(self) -> None:
        message = FakeMessage(1, text="Selling a car")
        await handle_message(FakeUpdate(100, message), SimpleNamespace())

        self.assertEqual(len(message.replies), 1)
        self.assertIn("make", message.replies[0].lower())
        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "awaiting_info")

    async def test_clarification_opens_negotiation_with_full_description(self) -> None:
        first = FakeMessage(1, text="Selling a car")
        await handle_message(FakeUpdate(100, first), SimpleNamespace())

        second = FakeMessage(2, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(101, second), SimpleNamespace())

        self.assertEqual(len(second.replies), 1)
        self.assertIn("EUR", second.replies[0])
        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "negotiating")
        # Full accumulated description must contain both messages (BUG-3 fix)
        self.assertIn("Selling a car", state.description)
        self.assertIn("2018 BMW", state.description)

    async def test_acceptance_resets_finished_negotiation(self) -> None:
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())

        accept = FakeMessage(2, text="Ok deal")
        await handle_message(FakeUpdate(101, accept), SimpleNamespace())

        self.assertTrue(any("agreed" in reply.lower() for reply in accept.replies))
        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "idle")

    async def test_deal_summary_sent_after_terminal_status(self) -> None:
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())

        accept = FakeMessage(2, text="Ok deal")
        await handle_message(FakeUpdate(101, accept), SimpleNamespace())

        # Negotiation reply + summary = at least 2 replies
        self.assertGreaterEqual(len(accept.replies), 2)
        summary = accept.replies[-1]
        self.assertTrue("agreed" in summary.lower() or "walk" in summary.lower())

    async def test_duplicate_message_is_ignored(self) -> None:
        message = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        update = FakeUpdate(100, message)
        await handle_message(update, SimpleNamespace())
        await handle_message(update, SimpleNamespace())
        # Second call is a no-op (idempotency via processed_messages table)
        self.assertEqual(len(message.replies), 1)

    async def test_captioned_listing_is_processed(self) -> None:
        message = FakeMessage(1, caption="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, message), SimpleNamespace())

        self.assertEqual(len(message.replies), 1)
        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "negotiating")

    async def test_reset_command_clears_active_session(self) -> None:
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())
        self.assertEqual(
            conversation.get_state(conversation.SessionKey(-123, 42, None)).phase, "negotiating"
        )

        reset_msg = FakeMessage(2, text="/reset")
        await reset_command(FakeUpdate(101, reset_msg), SimpleNamespace())

        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "idle")
        self.assertTrue(any("reset" in r.lower() for r in reset_msg.replies))

    async def test_cancel_command_behaves_like_reset(self) -> None:
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())

        cancel_msg = FakeMessage(2, text="/cancel")
        await cancel_command(FakeUpdate(101, cancel_msg), SimpleNamespace())

        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "idle")

    async def test_status_command_shows_idle_phase(self) -> None:
        status_msg = FakeMessage(1, text="/status")
        await status_command(FakeUpdate(100, status_msg), SimpleNamespace())
        self.assertIn("idle", status_msg.replies[0].lower())

    async def test_status_command_shows_negotiating_details(self) -> None:
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())

        status_msg = FakeMessage(2, text="/status")
        await status_command(FakeUpdate(101, status_msg), SimpleNamespace())
        self.assertIn("negotiating", status_msg.replies[0].lower())

    async def test_unauthorized_chat_is_ignored(self) -> None:
        msg = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        msg.chat_id = 99999
        await handle_message(FakeUpdate(100, msg), SimpleNamespace())
        self.assertEqual(len(msg.replies), 0)

    async def test_empty_text_is_ignored(self) -> None:
        msg = FakeMessage(1, text="   ")
        await handle_message(FakeUpdate(100, msg), SimpleNamespace())
        self.assertEqual(len(msg.replies), 0)

    async def test_rate_limiting_blocks_excess_messages(self) -> None:
        settings.RATE_LIMIT_MESSAGES = 2
        settings.RATE_LIMIT_WINDOW_SECONDS = 60
        th._rate_window.clear()

        for i in range(3):
            msg = FakeMessage(i + 1, text=f"2018 BMW 320d, 95k km, asking EUR 18000 msg{i}")
            await handle_message(FakeUpdate(100 + i, msg), SimpleNamespace())

        # Third message should be rate-limited (no negotiation reply, just rate warning)
        # We can check that the rate window has exactly 2 entries for user 42
        self.assertLessEqual(len(th._rate_window[42]), 2)

    async def test_turn_history_not_duplicated(self) -> None:
        """Seller's latest message must appear exactly once in state.history (BUG-2 fix)."""
        listing = FakeMessage(1, text="2018 BMW 320d, 95k km, good condition, asking EUR 18000")
        await handle_message(FakeUpdate(100, listing), SimpleNamespace())

        reply = FakeMessage(2, text="Can you do EUR 17000?")
        await handle_message(FakeUpdate(101, reply), SimpleNamespace())

        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        seller_turns = [t for t in state.history if t.speaker == "seller"]
        self.assertEqual(len(seller_turns), 1, "Seller turn must appear exactly once")

    async def test_message_with_photo_triggers_pricer(self) -> None:
        # User sends a photo with no text in the idle phase
        msg = FakeMessage(1)
        msg.photo = [FakePhotoSize()]
        
        await handle_message(FakeUpdate(100, msg), SimpleNamespace())
        
        # In demo mode, a photo without text simulates extracting BMW 320d registration specs
        # and starts negotiation
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("open at", msg.replies[0])
        state = conversation.get_state(conversation.SessionKey(-123, 42, None))
        self.assertEqual(state.phase, "negotiating")
        self.assertIn("BMW 320d", state.description)
        self.assertIn("libretto", state.description)


if __name__ == "__main__":
    unittest.main()
