"""
SQLite database models for the Slack agent.
Tables:
  - readai_calls: Meeting summaries received from Read.ai webhook
  - alerts_sent:  Dedup log for proactive alerts (prevents spam)
"""
import aiosqlite
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/app/data/tropical_bot.db")


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS readai_calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  TEXT UNIQUE,
                title       TEXT,
                date        TEXT,
                duration    INTEGER,
                participants TEXT,
                summary     TEXT,
                action_items TEXT,
                raw_payload TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_key   TEXT UNIQUE,
                channel     TEXT,
                message     TEXT,
                sent_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def upsert_readai_call(meeting_id: str, data: dict) -> bool:
    """Insert or ignore a Read.ai meeting call. Returns True if new."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO readai_calls
                (meeting_id, title, date, duration, participants, summary, action_items, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                data.get("title", ""),
                data.get("date", ""),
                data.get("duration", 0),
                data.get("participants", ""),
                data.get("summary", ""),
                data.get("action_items", ""),
                data.get("raw_payload", ""),
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def was_alert_sent(alert_key: str) -> bool:
    """Return True if this alert was already sent (prevents duplicates)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM alerts_sent WHERE alert_key = ?", (alert_key,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_alert_sent(alert_key: str, channel: str, message: str) -> None:
    """Record that an alert was sent."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO alerts_sent (alert_key, channel, message) VALUES (?, ?, ?)",
            (alert_key, channel, message),
        )
        await db.commit()


async def get_recent_meetings(limit: int = 10) -> list[dict]:
    """Return the most recent meetings from the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT meeting_id, title, date, duration, participants, summary, action_items, created_at
            FROM readai_calls
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def search_meetings(query: str, limit: int = 5) -> list[dict]:
    """Full-text search across title, summary and action_items."""
    pattern = f"%{query}%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT meeting_id, title, date, participants, summary, action_items, created_at
            FROM readai_calls
            WHERE title LIKE ? OR summary LIKE ? OR action_items LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
