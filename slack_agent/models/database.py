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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                key         TEXT PRIMARY KEY,
                token       TEXT NOT NULL,
                description TEXT,
                active      INTEGER DEFAULT 1,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                service       TEXT PRIMARY KEY,
                access_token  TEXT,
                refresh_token TEXT,
                expires_at    INTEGER,
                scope         TEXT,
                updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_reports (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client           TEXT,
                interval_minutes INTEGER NOT NULL,
                hours_back       INTEGER DEFAULT 24,
                channel          TEXT NOT NULL,
                active           INTEGER DEFAULT 1,
                last_run_at      DATETIME,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key  TEXT NOT NULL DEFAULT '',
                topic       TEXT NOT NULL DEFAULT 'geral',
                content     TEXT NOT NULL,
                source      TEXT DEFAULT 'agent',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_client ON memories(client_key)"
        )
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def add_workspace(key: str, token: str, description: str) -> bool:
    """Insert or replace a workspace. Returns True if new, False if updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM workspaces WHERE key = ?", (key,)) as cur:
            exists = await cur.fetchone() is not None
        await db.execute(
            "INSERT OR REPLACE INTO workspaces (key, token, description, active) VALUES (?, ?, ?, 1)",
            (key, token, description),
        )
        await db.commit()
        return not exists


async def remove_workspace(key: str) -> bool:
    """Delete a workspace. Returns True if it existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM workspaces WHERE key = ?", (key,))
        await db.commit()
        return cursor.rowcount > 0


async def list_db_workspaces() -> list[dict]:
    """Return all active workspaces stored in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT key, token, description, created_at FROM workspaces WHERE active = 1 ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_workspace(key: str) -> dict | None:
    """Return a single workspace by key, or None if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT key, token, description FROM workspaces WHERE key = ? AND active = 1", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_oauth_token(
    service: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    scope: str = "",
) -> None:
    """Store or update an OAuth token pair for a service."""
    import time
    expires_at = int(time.time()) + expires_in
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO oauth_tokens
                (service, access_token, refresh_token, expires_at, scope, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (service, access_token, refresh_token, expires_at, scope),
        )
        await db.commit()


async def get_oauth_token(service: str) -> dict | None:
    """Return stored OAuth tokens for a service, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT access_token, refresh_token, expires_at, scope FROM oauth_tokens WHERE service = ?",
            (service,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


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
            WHERE title LIKE ? OR summary LIKE ? OR action_items LIKE ? OR participants LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Memory (lightweight mempalace-style persistent memory)
# ---------------------------------------------------------------------------

async def save_memory(
    client_key: str,
    topic: str,
    content: str,
    source: str = "agent",
) -> int:
    """Save a memory entry. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO memories (client_key, topic, content, source) VALUES (?, ?, ?, ?)",
            (client_key.lower().strip(), topic.lower().strip(), content.strip(), source),
        )
        await db.commit()
        return cursor.lastrowid


async def recall_memories(
    client_key: str = "",
    topic: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Retrieve memories filtered by client, topic and/or query text.
    Empty client_key returns global (team-level) memories.
    """
    conditions = ["client_key = ?"]
    params: list = [client_key.lower().strip()]

    if topic:
        conditions.append("topic = ?")
        params.append(topic.lower().strip())

    if query:
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")

    where = " AND ".join(conditions)
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT id, client_key, topic, content, source, created_at
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def list_memory_topics(client_key: str = "") -> list[str]:
    """Return distinct topics stored for a client (or globally)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT topic FROM memories WHERE client_key = ? ORDER BY topic",
            (client_key.lower().strip(),),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def delete_memory(memory_id: int) -> bool:
    """Delete a memory by id. Returns True if it existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await db.commit()
        return cursor.rowcount > 0
