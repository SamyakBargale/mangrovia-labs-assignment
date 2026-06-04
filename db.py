"""
Async SQLite persistence layer using aiosqlite.

All public functions are coroutines and must be awaited. This keeps the
asyncio event loop free during DB operations instead of blocking it with
synchronous sqlite3 calls.

Connection lifecycle:
    Every function opens its own connection, runs its work inside a
    transaction, commits on success, rolls back on exception, and closes
    the connection via the aiosqlite context manager. Connections are
    never reused across calls — this is intentional for simplicity and
    avoids long-held file locks.

:memory: databases are explicitly unsupported because each call would get
a separate empty database.
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from config import settings


def now_iso() -> str:
    """Return a timezone-aware ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """
    Async context manager that opens a SQLite connection, enforces foreign
    keys, and commits/rolls back/closes automatically.

    Usage::

        async with get_connection() as conn:
            await conn.execute("SELECT 1")
    """
    if settings.DATABASE_PATH == ":memory:":
        raise ValueError(
            "DATABASE_PATH=:memory: is unsupported; use a temporary file path instead"
        )

    db_path = Path(settings.DATABASE_PATH)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(settings.DATABASE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _add_column_if_missing(
    conn: aiosqlite.Connection, table: str, column: str, definition: str
) -> None:
    """Add a column to an existing table if it does not already exist."""
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row["name"] for row in rows}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db() -> None:
    """
    Create all tables and apply additive column migrations.

    ``CREATE TABLE IF NOT EXISTS`` is idempotent so this can be called on
    every startup. Column additions use ``_add_column_if_missing`` so that
    existing databases gain new columns without a full schema rebuild.
    """
    async with get_connection() as conn:
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS car_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                raw_text TEXT,
                photo_path TEXT,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS estimates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_request_id INTEGER NOT NULL REFERENCES car_requests(id),
                status TEXT NOT NULL,
                low_price REAL,
                high_price REAL,
                currency TEXT,
                reasoning TEXT NOT NULL,
                raw_response TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS negotiations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                estimate_id INTEGER NOT NULL REFERENCES estimates(id),
                opening_message TEXT NOT NULL,
                opening_offer REAL,
                currency TEXT,
                posted_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                user_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                estimate_id INTEGER REFERENCES estimates(id),
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS negotiation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                telegram_message_id INTEGER,
                model_status TEXT,
                offered_price REAL,
                currency TEXT,
                raw_response TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                update_id INTEGER,
                processed_at TEXT NOT NULL,
                UNIQUE(chat_id, message_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_active
            ON sessions(chat_id, user_id, IFNULL(thread_id, -1))
            WHERE status = 'active';
            """
        )
        # Additive column migrations — safe to run every startup
        await _add_column_if_missing(conn, "car_requests", "thread_id", "INTEGER")
        await _add_column_if_missing(conn, "car_requests", "raw_text", "TEXT")
        await _add_column_if_missing(conn, "car_requests", "photo_path", "TEXT")
        await _add_column_if_missing(conn, "negotiations", "opening_offer", "REAL")
        await _add_column_if_missing(conn, "negotiations", "currency", "TEXT")


async def mark_message_processed(
    chat_id: int, message_id: int, update_id: int | None
) -> bool:
    """
    Record a Telegram message as processed for idempotency.

    Returns True if the message was newly inserted, False if it was already
    recorded (i.e. the update is a duplicate and should be skipped).
    """
    async with get_connection() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO processed_messages (chat_id, message_id, update_id, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, message_id, update_id, now_iso()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


async def get_or_create_session(
    chat_id: int,
    user_id: int,
    thread_id: int | None,
    phase: str,
    description: str = "",
) -> int:
    """
    Return the ID of the current active session for this chat/user/thread, or
    create a new one if none exists.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id FROM sessions
            WHERE chat_id = ?
              AND user_id = ?
              AND status = 'active'
              AND (thread_id IS ? OR thread_id = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, user_id, thread_id, thread_id),
        )
        row = await cursor.fetchone()
        if row:
            return int(row["id"])

        cursor = await conn.execute(
            """
            INSERT INTO sessions
                (chat_id, thread_id, user_id, phase, description, started_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, thread_id, user_id, phase, description, now_iso(), now_iso()),
        )
        return int(cursor.lastrowid)


async def update_session(
    session_id: int,
    phase: str,
    description: str,
    estimate_id: int | None = None,
    status: str = "active",
) -> None:
    """Update phase, description, estimate link, status, and activity timestamp for a session."""
    ended_at = now_iso() if status != "active" else None
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE sessions
            SET phase = ?,
                description = ?,
                estimate_id = ?,
                status = ?,
                last_activity_at = ?,
                ended_at = COALESCE(?, ended_at)
            WHERE id = ?
            """,
            (phase, description, estimate_id, status, now_iso(), ended_at, session_id),
        )


async def save_car_request(
    telegram_message_id: int,
    chat_id: int,
    user_id: int,
    description: str,
    thread_id: int | None = None,
    raw_text: str | None = None,
    photo_path: str | None = None,
) -> int:
    """Persist an incoming car listing. ``description`` is the full accumulated text."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO car_requests
                (telegram_message_id, chat_id, thread_id, user_id, description, raw_text, photo_path, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_message_id, chat_id, thread_id, user_id, description, raw_text, photo_path, now_iso()),
        )
        return int(cursor.lastrowid)


async def save_estimate(
    car_request_id: int,
    status: str,
    low_price: float | None,
    high_price: float | None,
    currency: str | None,
    reasoning: str,
    raw_response: str,
) -> int:
    """Persist a pricing estimate linked to a car request."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO estimates
                (car_request_id, status, low_price, high_price, currency, reasoning, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                car_request_id,
                status,
                low_price,
                high_price,
                currency,
                reasoning,
                raw_response,
                now_iso(),
            ),
        )
        return int(cursor.lastrowid)


async def save_negotiation(
    estimate_id: int,
    opening_message: str,
    opening_offer: float | None = None,
    currency: str | None = None,
) -> int:
    """Persist an opening negotiation record."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO negotiations
                (estimate_id, opening_message, opening_offer, currency, posted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (estimate_id, opening_message, opening_offer, currency, now_iso()),
        )
        return int(cursor.lastrowid)


async def save_turn(
    session_id: int,
    speaker: str,
    text: str,
    telegram_message_id: int | None = None,
    model_status: str | None = None,
    offered_price: float | None = None,
    currency: str | None = None,
    raw_response: str | None = None,
) -> int:
    """Persist a single negotiation turn (buyer or seller)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO negotiation_turns
                (session_id, speaker, text, telegram_message_id, model_status,
                 offered_price, currency, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                speaker,
                text,
                telegram_message_id,
                model_status,
                offered_price,
                currency,
                raw_response,
                now_iso(),
            ),
        )
        return int(cursor.lastrowid)
