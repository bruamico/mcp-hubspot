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
            CREATE TABLE IF NOT EXISTS channel_mappings (
                client_key   TEXT PRIMARY KEY,
                channel_name TEXT NOT NULL,
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_jobs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client           TEXT,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                channel          TEXT NOT NULL,
                min_priority     TEXT NOT NULL DEFAULT 'orange',
                active           INTEGER DEFAULT 1,
                last_run_at      DATETIME,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_snapshots (
                client_key   TEXT PRIMARY KEY,
                context      TEXT NOT NULL,
                captured_at  REAL NOT NULL,
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS commitments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key   TEXT NOT NULL,
                description  TEXT NOT NULL,
                requested_by TEXT,
                assigned_to  TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                priority     TEXT NOT NULL DEFAULT 'normal',
                due_date     TEXT,
                source_channel TEXT,
                source_ts    TEXT,
                fulfilled_at DATETIME,
                notes        TEXT,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_commitments_client ON commitments(client_key)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_commitments_source ON commitments(source_channel, source_ts)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS extraction_watermarks (
                channel_id  TEXT NOT NULL,
                workspace   TEXT NOT NULL DEFAULT 'internal',
                last_ts     TEXT NOT NULL,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, workspace)
            )
        """)

        # Migrations: safely add columns that may be missing in older DB instances.
        # ALTER TABLE ADD COLUMN fails if the column exists — catch and ignore.
        _col_migrations = [
            ("readai_calls", "title",        "TEXT"),
            ("readai_calls", "date",         "TEXT"),
            ("readai_calls", "duration",     "INTEGER DEFAULT 0"),
            ("readai_calls", "participants", "TEXT"),
            ("readai_calls", "summary",      "TEXT"),
            ("readai_calls", "action_items", "TEXT"),
            ("readai_calls", "raw_payload",  "TEXT"),
            ("readai_calls", "created_at",   "DATETIME DEFAULT '2000-01-01 00:00:00'"),
            ("memories",     "embedding",    "BLOB"),
        ]
        for table, col, col_type in _col_migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # column already exists — safe to ignore

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
    """Insert or update a Read.ai meeting call. Returns True if new."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM readai_calls WHERE meeting_id = ?", (meeting_id,)) as cur:
            exists = await cur.fetchone() is not None
        await db.execute(
            """
            INSERT INTO readai_calls
                (meeting_id, title, date, duration, participants, summary, action_items, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(meeting_id) DO UPDATE SET
                title        = excluded.title,
                date         = excluded.date,
                duration     = excluded.duration,
                participants = excluded.participants,
                summary      = excluded.summary,
                action_items = excluded.action_items,
                raw_payload  = excluded.raw_payload
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
        return not exists


async def reprocess_raw_payloads() -> int:
    """Re-parse raw_payload for all meetings with empty title. Returns count updated."""
    import json as _json

    def _extract(payload: dict) -> dict:
        meeting = (
            payload.get("meeting")
            or payload.get("data", {}).get("meeting")
            or payload.get("data")
            or payload
        )
        summary_raw = meeting.get("summary") or meeting.get("transcript_summary") or {}
        if isinstance(summary_raw, dict):
            summary_text = summary_raw.get("overview") or summary_raw.get("text") or ""
            ai_raw = summary_raw.get("action_items") or meeting.get("action_items") or []
        else:
            summary_text = str(summary_raw) if summary_raw else ""
            ai_raw = meeting.get("action_items") or []
        participants_raw = meeting.get("participants") or []
        if isinstance(participants_raw, list):
            participants = ", ".join(
                (p.get("name") or p.get("email") or "") if isinstance(p, dict) else str(p)
                for p in participants_raw
            )
        else:
            participants = str(participants_raw)
        if isinstance(ai_raw, list):
            action_items = "\n".join(
                f"- {a.get('text', str(a))}" if isinstance(a, dict) else f"- {a}"
                for a in ai_raw
            )
        else:
            action_items = str(ai_raw) if ai_raw else ""
        return {
            "title": meeting.get("title") or meeting.get("name") or "",
            "date": meeting.get("date") or meeting.get("start_time") or meeting.get("created_at") or "",
            "duration": meeting.get("duration") or 0,
            "participants": participants,
            "summary": summary_text,
            "action_items": action_items,
        }

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT meeting_id, raw_payload FROM readai_calls WHERE (title IS NULL OR title = '') AND raw_payload IS NOT NULL AND raw_payload != ''"
        ) as cur:
            rows = await cur.fetchall()

        updated = 0
        for row in rows:
            try:
                payload = _json.loads(row["raw_payload"])
                data = _extract(payload)
                if data.get("title") or data.get("summary"):
                    await db.execute(
                        """UPDATE readai_calls SET title=?, date=?, duration=?, participants=?, summary=?, action_items=?
                           WHERE meeting_id=?""",
                        (data["title"], data["date"], data["duration"],
                         data["participants"], data["summary"], data["action_items"],
                         row["meeting_id"]),
                    )
                    updated += 1
            except Exception:
                pass
        await db.commit()
    logger.info("reprocess_raw_payloads: updated %d records", updated)
    return updated


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


async def search_meetings(query: str, limit: int = 20, since_iso: str | None = None) -> list[dict]:
    """Full-text search across title, summary, action_items and participants.

    Args:
        query: Keyword to search for (LIKE match).
        limit: Maximum number of results *after* time filtering.
        since_iso: ISO date string (YYYY-MM-DD or full ISO) to filter meetings on or after.
    """
    pattern = f"%{query}%"
    base_sql = """
        SELECT meeting_id, title, date, participants, summary, action_items, created_at
        FROM readai_calls
        WHERE (title LIKE ? OR summary LIKE ? OR action_items LIKE ? OR participants LIKE ?)
    """
    params: list = [pattern, pattern, pattern, pattern]

    if since_iso:
        base_sql += " AND (date >= ? OR (date IS NULL AND created_at >= ?))"
        params += [since_iso[:10], since_iso[:10]]

    base_sql += " ORDER BY COALESCE(date, created_at) DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(base_sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_meetings_in_window(since_iso: str, until_iso: str | None = None, limit: int = 50) -> list[dict]:
    """Return all meetings within a date range, ordered by meeting date descending.

    Args:
        since_iso: Start of the window (YYYY-MM-DD or full ISO). Inclusive.
        until_iso: End of the window (optional). Defaults to now.
        limit: Maximum rows returned.
    """
    conditions = ["(date >= ? OR (date IS NULL AND created_at >= ?))"]
    params: list = [since_iso[:10], since_iso[:10]]

    if until_iso:
        conditions.append("(date <= ? OR (date IS NULL AND created_at <= ?))")
        params += [until_iso[:10], until_iso[:10]]

    sql = (
        "SELECT meeting_id, title, date, duration, participants, summary, action_items, created_at "
        "FROM readai_calls WHERE "
        + " AND ".join(conditions)
        + " ORDER BY COALESCE(date, created_at) DESC LIMIT ?"
    )
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
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
    embedding: bytes | None = None,
) -> int:
    """Save a memory entry. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO memories (client_key, topic, content, source, embedding) VALUES (?, ?, ?, ?, ?)",
            (client_key.lower().strip(), topic.lower().strip(), content.strip(), source, embedding),
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


async def fetch_memories_with_embeddings(
    client_key: str = "",
    topic: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Return memories including their embedding blobs.
    Used by semantic recall — loads all candidates so we can rank by similarity.
    """
    conditions = ["client_key = ?"]
    params: list = [client_key.lower().strip()]
    if topic:
        conditions.append("topic = ?")
        params.append(topic.lower().strip())
    where = " AND ".join(conditions)
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT id, client_key, topic, content, source, embedding, created_at
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


# ---------------------------------------------------------------------------
# Channel mappings (client_key → internal Tropical Hub channel name)
# ---------------------------------------------------------------------------

async def set_channel_mapping(client_key: str, channel_name: str) -> None:
    """Save or update the internal channel name for a client key."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO channel_mappings (client_key, channel_name, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (client_key.lower().strip(), channel_name.lstrip("#").lower().strip()),
        )
        await db.commit()


async def get_channel_mapping(client_key: str) -> str | None:
    """Return the mapped channel name for a client key, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_name FROM channel_mappings WHERE client_key = ?",
            (client_key.lower().strip(),),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_channel_mappings() -> list[dict]:
    """Return all stored channel mappings."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT client_key, channel_name, updated_at FROM channel_mappings ORDER BY client_key"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Monitoring jobs + snapshots
# ---------------------------------------------------------------------------

async def add_monitoring_job(
    client: str | None,
    interval_minutes: int,
    channel: str,
    min_priority: str = "orange",
) -> int:
    """Create a new monitoring job. Returns the new job id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO monitoring_jobs
               (client, interval_minutes, channel, min_priority, active)
               VALUES (?, ?, ?, ?, 1)""",
            (client, interval_minutes, channel, min_priority),
        )
        await db.commit()
        return cur.lastrowid


async def remove_monitoring_job(job_id: int) -> bool:
    """Deactivate a monitoring job. Returns True if found."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE monitoring_jobs SET active=0 WHERE id=?", (job_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def list_monitoring_jobs() -> list[dict]:
    """Return all active monitoring jobs."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, client, interval_minutes, channel, min_priority,
                      last_run_at, created_at
               FROM monitoring_jobs WHERE active=1 ORDER BY id"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_monitoring_job_last_run(job_id: int) -> None:
    """Update last_run_at to now for a monitoring job."""
    from datetime import datetime, timezone
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitoring_jobs SET last_run_at=? WHERE id=?", (now_iso, job_id)
        )
        await db.commit()


async def get_monitoring_snapshot(client_key: str) -> dict | None:
    """Return the last saved snapshot for a client, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT client_key, context, captured_at FROM monitoring_snapshots WHERE client_key=?",
            (client_key,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def save_monitoring_snapshot(client_key: str, context: str, captured_at: float) -> None:
    """Insert or update a monitoring snapshot."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO monitoring_snapshots
               (client_key, context, captured_at, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
            (client_key, context, captured_at),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Commitments (task/request tracker)
# ---------------------------------------------------------------------------

async def add_commitment(
    client_key: str,
    description: str,
    requested_by: str = "",
    assigned_to: str = "",
    priority: str = "normal",
    due_date: str = "",
    source_channel: str = "",
    source_ts: str = "",
    msg_created_at: str | None = None,
) -> int:
    """Create a new commitment. Returns the new row id.

    Pass msg_created_at (ISO datetime string) to backdate the created_at field
    to the original Slack message timestamp so age calculations are accurate.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if msg_created_at:
            cur = await db.execute(
                """INSERT INTO commitments
                   (client_key, description, requested_by, assigned_to, priority,
                    due_date, source_channel, source_ts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_key.lower().strip(), description.strip(),
                 requested_by.strip(), assigned_to.strip(), priority.lower().strip(),
                 due_date.strip(), source_channel.strip(), source_ts.strip(),
                 msg_created_at, msg_created_at),
            )
        else:
            cur = await db.execute(
                """INSERT INTO commitments
                   (client_key, description, requested_by, assigned_to, priority,
                    due_date, source_channel, source_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_key.lower().strip(), description.strip(),
                 requested_by.strip(), assigned_to.strip(), priority.lower().strip(),
                 due_date.strip(), source_channel.strip(), source_ts.strip()),
            )
        await db.commit()
        return cur.lastrowid


async def update_commitment(
    commitment_id: int,
    status: str | None = None,
    notes: str | None = None,
    assigned_to: str | None = None,
    due_date: str | None = None,
) -> bool:
    """Update fields on a commitment. Returns True if found."""
    fields, params = [], []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
        if status == "done":
            fields.append("fulfilled_at = CURRENT_TIMESTAMP")
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if assigned_to is not None:
        fields.append("assigned_to = ?")
        params.append(assigned_to)
    if due_date is not None:
        fields.append("due_date = ?")
        params.append(due_date)
    if not fields:
        return False
    fields.append("updated_at = CURRENT_TIMESTAMP")
    params.append(commitment_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE commitments SET {', '.join(fields)} WHERE id = ?", params
        )
        await db.commit()
        return cur.rowcount > 0


async def list_commitments(
    client_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List commitments filtered by client and/or status."""
    conditions, params = [], []
    if client_key:
        conditions.append("client_key = ?")
        params.append(client_key.lower().strip())
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT id, client_key, description, requested_by, assigned_to,
                       status, priority, due_date, source_channel, notes,
                       fulfilled_at, created_at, updated_at
                FROM commitments {where}
                ORDER BY
                    CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                  WHEN 'normal' THEN 2 ELSE 3 END,
                    created_at ASC
                LIMIT ?""",
            params,
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def commitment_source_exists(source_channel: str, source_ts: str) -> bool:
    """Return True if a commitment with this exact source already exists (dedup)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM commitments WHERE source_channel = ? AND source_ts = ?",
            (source_channel, source_ts),
        ) as cur:
            return await cur.fetchone() is not None


async def get_watermark(channel_id: str, workspace: str = "internal") -> str | None:
    """Return the last processed Slack ts for a channel, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_ts FROM extraction_watermarks WHERE channel_id = ? AND workspace = ?",
            (channel_id, workspace),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_watermark(channel_id: str, workspace: str, last_ts: str) -> None:
    """Update the watermark for a channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO extraction_watermarks (channel_id, workspace, last_ts, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
            (channel_id, workspace, last_ts),
        )
        await db.commit()


async def get_overdue_commitments(days_old: int = 3) -> list[dict]:
    """Return pending commitments older than N days."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, client_key, description, requested_by, assigned_to,
                      priority, due_date, created_at
               FROM commitments
               WHERE status = 'pending'
                 AND created_at <= datetime('now', ? || ' days')
               ORDER BY client_key, created_at ASC""",
            (f"-{days_old}",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_due_soon_commitments(hours_ahead: int = 24) -> list[dict]:
    """Return pending commitments whose due_date is within the next N hours."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, client_key, description, requested_by, assigned_to,
                      priority, due_date, created_at
               FROM commitments
               WHERE status = 'pending'
                 AND due_date IS NOT NULL AND due_date != ''
                 AND due_date <= date('now', ? || ' hours')
                 AND due_date >= date('now')
               ORDER BY due_date ASC, client_key ASC""",
            (f"+{hours_ahead}",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
