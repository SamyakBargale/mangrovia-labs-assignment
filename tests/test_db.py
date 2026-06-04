"""
Tests for the async SQLite persistence layer (db.py).

All tests use IsolatedAsyncioTestCase so that the async db functions can be
awaited naturally. A fresh temporary database file is created for each test
class and removed on teardown — no :memory: or shared state between tests.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-123")
os.environ.setdefault("DEMO_MODE", "true")

import db
from config import settings


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings.DATABASE_PATH = str(Path(self.tmp.name) / "test.db")
        await db.init_db()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_connection_works_and_closes(self) -> None:
        """Basic sanity: get_connection() opens and the context manager closes cleanly."""
        async with db.get_connection() as conn:
            cursor = await conn.execute("SELECT 1")
            row = await cursor.fetchone()
        self.assertEqual(row[0], 1)

    async def test_foreign_keys_are_enforced(self) -> None:
        """Inserting an estimate with a nonexistent car_request_id must fail."""
        with self.assertRaises(sqlite3.IntegrityError):
            await db.save_estimate(
                car_request_id=999,
                status="estimated",
                low_price=10000,
                high_price=12000,
                currency="EUR",
                reasoning="orphan",
                raw_response="{}",
            )

    async def test_processed_message_idempotency(self) -> None:
        """First insert returns True; duplicate returns False."""
        self.assertTrue(
            await db.mark_message_processed(chat_id=-123, message_id=1, update_id=10)
        )
        self.assertFalse(
            await db.mark_message_processed(chat_id=-123, message_id=1, update_id=10)
        )

    async def test_full_request_estimate_turn_flow(self) -> None:
        """End-to-end: session → car_request → estimate → session update → turn."""
        session_id = await db.get_or_create_session(-123, 42, None, "idle")
        request_id = await db.save_car_request(
            1, -123, 42, "2018 BMW 320d 95k km", raw_text="2018 BMW 320d 95k km"
        )
        estimate_id = await db.save_estimate(
            request_id, "estimated", 10000, 12000, "EUR", "ok", "{}"
        )
        await db.update_session(session_id, "negotiating", "2018 BMW 320d 95k km", estimate_id)
        turn_id = await db.save_turn(
            session_id, "buyer", "Would you take EUR 9,200?",
            offered_price=9200, currency="EUR",
        )
        self.assertGreater(turn_id, 0)

    async def test_completed_thread_sessions_can_repeat(self) -> None:
        """After closing a session a new one for the same key can be created."""
        first = await db.get_or_create_session(-123, 42, 7, "idle")
        await db.update_session(first, "idle", "", None, status="deal_agreed")
        second = await db.get_or_create_session(-123, 42, 7, "idle")
        await db.update_session(second, "idle", "", None, status="deal_agreed")
        self.assertNotEqual(first, second)

    async def test_get_or_create_returns_existing_active_session(self) -> None:
        """Calling get_or_create_session twice returns the same ID."""
        first = await db.get_or_create_session(-123, 42, None, "idle")
        second = await db.get_or_create_session(-123, 42, None, "idle")
        self.assertEqual(first, second)

    async def test_save_negotiation_round_trip(self) -> None:
        """Opening negotiation record persists without error."""
        request_id = await db.save_car_request(1, -123, 42, "test car")
        estimate_id = await db.save_estimate(
            request_id, "estimated", 8000, 10000, "EUR", "ok", "{}"
        )
        neg_id = await db.save_negotiation(
            estimate_id, "Opening offer text", opening_offer=7000, currency="EUR"
        )
        self.assertGreater(neg_id, 0)

    async def test_all_tables_created(self) -> None:
        """init_db must create all six expected tables."""
        async with db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            rows = await cursor.fetchall()
        tables = {row["name"] for row in rows}
        expected = {
            "car_requests", "estimates", "negotiations",
            "sessions", "negotiation_turns", "processed_messages",
        }
        self.assertTrue(expected.issubset(tables), f"Missing tables: {expected - tables}")


if __name__ == "__main__":
    unittest.main()
