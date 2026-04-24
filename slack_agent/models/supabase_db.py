"""
Supabase-backed persistence layer for the Slack agent.

Drop-in replacement for the aiosqlite-based functions in database.py.
Activated automatically when SUPABASE_URL env var is set — database.py
re-exports these functions at module load time.

Schema: run supabase_schema.sql in the Supabase SQL Editor once.
"""
import logging
import os
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Module-level singleton — lazy-initialized on first use
_client = None


async def _sb():
    """Return the Supabase AsyncClient, initialising it on first call."""
    global _client
    if _client is None:
        from supabase import create_async_client
        _client = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ---------------------------------------------------------------------------
# Meeting format helpers
# ---------------------------------------------------------------------------

def _participants_to_str(value) -> str:
    """Convert JSONB participants (list of dicts) to comma-separated names."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        names = []
        for p in value:
            if isinstance(p, dict):
                names.append(str(p.get("name") or p.get("email") or ""))
            else:
                names.append(str(p))
        return ", ".join(n for n in names if n)
    return str(value)


def _action_items_to_str(value) -> str:
    """Convert JSONB action_items (list of dicts/strings) to text lines."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        lines = []
        for a in value:
            if isinstance(a, dict):
                text = a.get("text") or a.get("content") or str(a)
                assignee = a.get("assignee") or a.get("owner") or ""
                lines.append(f"- {text}" + (f" ({assignee})" if assignee else ""))
            else:
                lines.append(f"- {a}")
        return "\n".join(lines)
    return str(value)


def _meeting_row(r: dict) -> dict:
    """Normalise a Supabase meetings row to the internal format used by the rest of the code."""
    return {
        "meeting_id": r.get("meeting_id") or str(r.get("id", "")),
        "title":       r.get("title") or "",
        # Return the ISO date string; truncate fractional seconds for readability
        "date":        (r.get("meeting_date") or r.get("created_at") or "")[:19],
        "duration":    r.get("duration_seconds") or 0,
        "participants": _participants_to_str(r.get("participants")),
        "summary":     r.get("summary") or "",
        "action_items": _action_items_to_str(r.get("action_items")),
        "created_at":  r.get("created_at") or "",
        # Rich fields only available via Supabase
        "key_questions": r.get("key_questions"),
        "topics":        r.get("topics"),
        "recording_url": r.get("recording_url"),
    }


# ---------------------------------------------------------------------------
# init_db — no-op; schema is managed via supabase_schema.sql
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Verify Supabase connectivity. Schema must be created via supabase_schema.sql."""
    try:
        sb = await _sb()
        await sb.table("meetings").select("meeting_id").limit(1).execute()
        logger.info("Supabase backend active (%s)", SUPABASE_URL)
    except Exception as exc:
        logger.error("Supabase connectivity check failed: %s", exc)


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

async def add_workspace(key: str, token: str, description: str) -> bool:
    sb = await _sb()
    result = await sb.table("workspaces").select("key").eq("key", key).execute()
    exists = bool(result.data)
    await sb.table("workspaces").upsert(
        {"key": key, "token": token, "description": description, "active": True}
    ).execute()
    return not exists


async def remove_workspace(key: str) -> bool:
    sb = await _sb()
    result = await sb.table("workspaces").select("key").eq("key", key).execute()
    if not result.data:
        return False
    await sb.table("workspaces").update({"active": False}).eq("key", key).execute()
    return True


async def list_db_workspaces() -> list[dict]:
    sb = await _sb()
    result = await sb.table("workspaces").select(
        "key,token,description,created_at"
    ).eq("active", True).order("created_at").execute()
    return result.data or []


async def get_workspace(key: str) -> dict | None:
    sb = await _sb()
    result = await sb.table("workspaces").select(
        "key,token,description"
    ).eq("key", key).eq("active", True).execute()
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------
# OAuth tokens
# ---------------------------------------------------------------------------

async def save_oauth_token(
    service: str, access_token: str, refresh_token: str,
    expires_in: int, scope: str = "",
) -> None:
    expires_at = int(time.time()) + expires_in
    sb = await _sb()
    await sb.table("oauth_tokens").upsert({
        "service":       service,
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "expires_at":    expires_at,
        "scope":         scope,
        "updated_at":    datetime.now(tz=timezone.utc).isoformat(),
    }).execute()


async def get_oauth_token(service: str) -> dict | None:
    sb = await _sb()
    result = await sb.table("oauth_tokens").select(
        "access_token,refresh_token,expires_at,scope"
    ).eq("service", service).execute()
    return result.data[0] if result.data else None


# ---------------------------------------------------------------------------
# Read.ai meetings
# ---------------------------------------------------------------------------

async def upsert_readai_call(meeting_id: str, data: dict) -> bool:
    """
    Insert a meeting into Supabase.

    If the meeting already exists (stored by the Edge Function with richer
    JSONB data), we skip the upsert to avoid downgrading the schema.
    Returns True only when a new row was created.
    """
    sb = await _sb()
    result = await sb.table("meetings").select("meeting_id").eq(
        "meeting_id", meeting_id
    ).execute()
    if result.data:
        return False  # Edge Function already captured this; don't overwrite

    await sb.table("meetings").insert({
        "meeting_id":       meeting_id,
        "title":            data.get("title", ""),
        "meeting_date":     data.get("date") or None,
        "duration_seconds": data.get("duration") or 0,
        # Store as plain text inside a JSONB column — Supabase accepts a string
        "participants":     data.get("participants", ""),
        "summary":          data.get("summary", ""),
        "action_items":     data.get("action_items", ""),
        "raw_payload":      data.get("raw_payload", ""),
    }).execute()
    return True


async def get_recent_meetings(limit: int = 10) -> list[dict]:
    sb = await _sb()
    result = await sb.table("meetings").select(
        "meeting_id,title,meeting_date,duration_seconds,participants,"
        "summary,action_items,key_questions,topics,recording_url,created_at"
    ).order("meeting_date", desc=True).limit(limit).execute()
    return [_meeting_row(r) for r in (result.data or [])]


async def search_meetings(
    query: str, limit: int = 20, since_iso: str | None = None
) -> list[dict]:
    """
    Keyword search across title, summary, participants and action_items.
    Uses the search_meetings_fn PostgreSQL function for JSONB-aware ILIKE.
    """
    sb = await _sb()
    params: dict = {"q": query, "lim": limit}
    if since_iso:
        params["since_dt"] = since_iso[:10]
    result = await sb.rpc("search_meetings_fn", params).execute()
    return [_meeting_row(r) for r in (result.data or [])]


async def get_meetings_in_window(
    since_iso: str, until_iso: str | None = None, limit: int = 50
) -> list[dict]:
    sb = await _sb()
    since = since_iso[:10]
    # Include rows where meeting_date is NULL (fall back to created_at for those)
    date_filter = f"meeting_date.gte.{since},and(meeting_date.is.null,created_at.gte.{since})"
    builder = sb.table("meetings").select(
        "meeting_id,title,meeting_date,duration_seconds,participants,"
        "summary,action_items,key_questions,topics,recording_url,created_at"
    ).or_(date_filter)
    if until_iso:
        until = until_iso[:10]
        builder = builder.or_(
            f"meeting_date.lte.{until},and(meeting_date.is.null,created_at.lte.{until})"
        )
    result = await builder.order("meeting_date", desc=True, nullsfirst=False).limit(limit).execute()
    return [_meeting_row(r) for r in (result.data or [])]


async def reprocess_raw_payloads() -> int:
    """No-op — the Edge Function handles rich JSONB parsing."""
    logger.info("reprocess_raw_payloads: skipped (handled by Supabase Edge Function)")
    return 0


# ---------------------------------------------------------------------------
# Alerts dedup
# ---------------------------------------------------------------------------

async def was_alert_sent(alert_key: str) -> bool:
    sb = await _sb()
    result = await sb.table("alerts_sent").select("id").eq(
        "alert_key", alert_key
    ).execute()
    return bool(result.data)


async def mark_alert_sent(alert_key: str, channel: str, message: str) -> None:
    sb = await _sb()
    await sb.table("alerts_sent").upsert({
        "alert_key": alert_key,
        "channel":   channel,
        "message":   message[:2000],
    }).execute()


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

async def save_memory(
    client_key: str, topic: str, content: str,
    source: str = "agent", embedding: bytes | None = None,
) -> int:
    sb = await _sb()
    data: dict = {
        "client_key": client_key.lower().strip(),
        "topic":      topic.lower().strip(),
        "content":    content.strip(),
        "source":     source,
    }
    # embedding arrives as raw bytes (struct.pack of floats) from SQLite callers,
    # but memory_service._save_with_embedding inserts directly with list[float].
    # Only convert if bytes were passed here (legacy path).
    if isinstance(embedding, (bytes, bytearray)) and len(embedding) > 0:
        import struct
        n = len(embedding) // 4
        data["embedding"] = list(struct.unpack(f"{n}f", embedding))
    result = await sb.table("memories").insert(data).execute()
    return result.data[0]["id"] if result.data else 0


async def recall_memories(
    client_key: str = "",
    topic: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[dict]:
    sb = await _sb()
    builder = sb.table("memories").select(
        "id,client_key,topic,content,source,created_at"
    ).eq("client_key", client_key.lower().strip())
    if topic:
        builder = builder.eq("topic", topic.lower().strip())
    if query:
        builder = builder.ilike("content", f"%{query}%")
    result = await builder.order("created_at", desc=True).limit(limit).execute()
    return result.data or []


async def fetch_memories_with_embeddings(
    client_key: str = "", topic: str | None = None, limit: int = 200
) -> list[dict]:
    sb = await _sb()
    builder = sb.table("memories").select(
        "id,client_key,topic,content,source,created_at"
    ).eq("client_key", client_key.lower().strip())
    if topic:
        builder = builder.eq("topic", topic.lower().strip())
    result = await builder.order("created_at", desc=True).limit(limit).execute()
    rows = result.data or []
    for r in rows:
        r.setdefault("embedding", None)  # vector not returned via REST
    return rows


async def list_memory_topics(client_key: str = "") -> list[str]:
    sb = await _sb()
    result = await sb.table("memories").select("topic").eq(
        "client_key", client_key.lower().strip()
    ).execute()
    seen: set[str] = set()
    topics: list[str] = []
    for r in (result.data or []):
        t = r.get("topic", "")
        if t and t not in seen:
            seen.add(t)
            topics.append(t)
    return sorted(topics)


async def delete_memory(memory_id: int) -> bool:
    sb = await _sb()
    result = await sb.table("memories").select("id").eq("id", memory_id).execute()
    if not result.data:
        return False
    await sb.table("memories").delete().eq("id", memory_id).execute()
    return True


# ---------------------------------------------------------------------------
# Channel mappings
# ---------------------------------------------------------------------------

async def set_channel_mapping(client_key: str, channel_name: str) -> None:
    sb = await _sb()
    await sb.table("channel_mappings").upsert({
        "client_key":   client_key.lower().strip(),
        "channel_name": channel_name.lstrip("#").lower().strip(),
        "updated_at":   datetime.now(tz=timezone.utc).isoformat(),
    }).execute()


async def get_channel_mapping(client_key: str) -> str | None:
    sb = await _sb()
    result = await sb.table("channel_mappings").select("channel_name").eq(
        "client_key", client_key.lower().strip()
    ).execute()
    return result.data[0]["channel_name"] if result.data else None


async def list_channel_mappings() -> list[dict]:
    sb = await _sb()
    result = await sb.table("channel_mappings").select(
        "client_key,channel_name,updated_at"
    ).order("client_key").execute()
    return result.data or []


# ---------------------------------------------------------------------------
# Monitoring jobs + snapshots
# ---------------------------------------------------------------------------

async def add_monitoring_job(
    client: str | None, interval_minutes: int,
    channel: str, min_priority: str = "orange",
) -> int:
    sb = await _sb()
    result = await sb.table("monitoring_jobs").insert({
        "client":           client,
        "interval_minutes": interval_minutes,
        "channel":          channel,
        "min_priority":     min_priority,
        "active":           True,
    }).execute()
    return result.data[0]["id"] if result.data else 0


async def remove_monitoring_job(job_id: int) -> bool:
    sb = await _sb()
    result = await sb.table("monitoring_jobs").select("id").eq("id", job_id).execute()
    if not result.data:
        return False
    await sb.table("monitoring_jobs").update({"active": False}).eq("id", job_id).execute()
    return True


async def list_monitoring_jobs() -> list[dict]:
    sb = await _sb()
    result = await sb.table("monitoring_jobs").select(
        "id,client,interval_minutes,channel,min_priority,last_run_at,created_at"
    ).eq("active", True).order("id").execute()
    return result.data or []


async def update_monitoring_job_last_run(job_id: int) -> None:
    sb = await _sb()
    await sb.table("monitoring_jobs").update({
        "last_run_at": datetime.now(tz=timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


async def get_monitoring_snapshot(client_key: str) -> dict | None:
    sb = await _sb()
    result = await sb.table("monitoring_snapshots").select(
        "client_key,context,captured_at"
    ).eq("client_key", client_key).execute()
    return result.data[0] if result.data else None


async def save_monitoring_snapshot(
    client_key: str, context: str, captured_at: float
) -> None:
    sb = await _sb()
    await sb.table("monitoring_snapshots").upsert({
        "client_key":  client_key,
        "context":     context,
        "captured_at": captured_at,
        "updated_at":  datetime.now(tz=timezone.utc).isoformat(),
    }).execute()


# ---------------------------------------------------------------------------
# Commitments
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
    sb = await _sb()
    row: dict = {
        "client_key":     client_key.lower().strip(),
        "description":    description.strip()[:500],
        "requested_by":   requested_by.strip(),
        "assigned_to":    assigned_to.strip(),
        "priority":       priority.lower().strip(),
        "due_date":       due_date.strip() or None,
        "source_channel": source_channel.strip() or None,
        "source_ts":      source_ts.strip() or None,
    }
    if msg_created_at:
        row["created_at"] = msg_created_at
        row["updated_at"] = msg_created_at
    result = await sb.table("commitments").insert(row).execute()
    return result.data[0]["id"] if result.data else 0


async def update_commitment(
    commitment_id: int,
    status: str | None = None,
    notes: str | None = None,
    assigned_to: str | None = None,
    due_date: str | None = None,
) -> bool:
    updates: dict = {}
    if status is not None:
        updates["status"] = status
        if status == "done":
            updates["fulfilled_at"] = datetime.now(tz=timezone.utc).isoformat()
    if notes is not None:
        updates["notes"] = notes
    if assigned_to is not None:
        updates["assigned_to"] = assigned_to
    if due_date is not None:
        updates["due_date"] = due_date or None
    if not updates:
        return False
    updates["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    sb = await _sb()
    result = await sb.table("commitments").update(updates).eq(
        "id", commitment_id
    ).execute()
    return bool(result.data)


async def list_commitments(
    client_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sb = await _sb()
    builder = sb.table("commitments").select(
        "id,client_key,description,requested_by,assigned_to,"
        "status,priority,due_date,source_channel,notes,fulfilled_at,created_at,updated_at"
    )
    if client_key:
        builder = builder.eq("client_key", client_key.lower().strip())
    if status:
        builder = builder.eq("status", status)
    result = await builder.order("created_at", desc=False).limit(limit).execute()
    rows = result.data or []
    # PostgREST doesn't support CASE ordering — sort by priority in Python
    _prio = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    rows.sort(key=lambda r: (_prio.get(r.get("priority", "normal"), 2), r.get("created_at", "")))
    return rows


async def commitment_source_exists(source_channel: str, source_ts: str) -> bool:
    sb = await _sb()
    result = await sb.table("commitments").select("id").eq(
        "source_channel", source_channel
    ).eq("source_ts", source_ts).execute()
    return bool(result.data)


async def get_watermark(channel_id: str, workspace: str = "internal") -> str | None:
    sb = await _sb()
    result = await sb.table("extraction_watermarks").select("last_ts").eq(
        "channel_id", channel_id
    ).eq("workspace", workspace).execute()
    return result.data[0]["last_ts"] if result.data else None


async def set_watermark(channel_id: str, workspace: str, last_ts: str) -> None:
    sb = await _sb()
    await sb.table("extraction_watermarks").upsert({
        "channel_id": channel_id,
        "workspace":  workspace,
        "last_ts":    last_ts,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }).execute()


async def get_overdue_commitments(days_old: int = 3) -> list[dict]:
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days_old)).isoformat()
    sb = await _sb()
    result = await sb.table("commitments").select(
        "id,client_key,description,requested_by,assigned_to,priority,due_date,created_at"
    ).eq("status", "pending").lte("created_at", cutoff).order("client_key").order(
        "created_at"
    ).execute()
    return result.data or []


async def get_due_soon_commitments(hours_ahead: int = 24) -> list[dict]:
    today  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=hours_ahead)).strftime("%Y-%m-%d")
    sb = await _sb()
    result = await sb.table("commitments").select(
        "id,client_key,description,requested_by,assigned_to,priority,due_date,created_at"
    ).eq("status", "pending").not_.is_("due_date", "null").lte(
        "due_date", future
    ).gte("due_date", today).order("due_date").order("client_key").execute()
    return result.data or []


# ---------------------------------------------------------------------------
# Scheduled reports
# ---------------------------------------------------------------------------

async def list_scheduled_reports() -> list[dict]:
    sb = await _sb()
    result = await sb.table("scheduled_reports").select(
        "id,client,interval_minutes,hours_back,channel,active,last_run_at,created_at"
    ).eq("active", True).order("id").execute()
    return result.data or []


async def add_scheduled_report(
    client: str | None, interval_minutes: int, hours_back: int, channel: str
) -> int:
    sb = await _sb()
    result = await sb.table("scheduled_reports").insert({
        "client":           client,
        "interval_minutes": interval_minutes,
        "hours_back":       hours_back,
        "channel":          channel,
        "active":           True,
    }).execute()
    return result.data[0]["id"] if result.data else 0


async def delete_scheduled_report(report_id: int) -> bool:
    sb = await _sb()
    result = await sb.table("scheduled_reports").select("id").eq("id", report_id).execute()
    if not result.data:
        return False
    await sb.table("scheduled_reports").update({"active": False}).eq(
        "id", report_id
    ).execute()
    return True


async def update_scheduled_report_last_run(report_id: int) -> None:
    sb = await _sb()
    await sb.table("scheduled_reports").update({
        "last_run_at": datetime.now(tz=timezone.utc).isoformat(),
    }).eq("id", report_id).execute()


# ---------------------------------------------------------------------------
# RBAC — User permissions
# ---------------------------------------------------------------------------

async def get_user_role(slack_user_id: str) -> str:
    """Return the role for a Slack user: 'admin' or 'user' (default).
    ADMIN_SLACK_USER_ID env var bootstraps the first admin without a DB entry."""
    import os as _os
    if slack_user_id and slack_user_id == _os.getenv("ADMIN_SLACK_USER_ID", ""):
        return "admin"
    try:
        sb = await _sb()
        result = await sb.table("user_permissions").select("role").eq(
            "slack_user_id", slack_user_id
        ).execute()
        if result.data:
            return result.data[0]["role"]
    except Exception as exc:
        logger.warning("get_user_role failed: %s", exc)
    return "user"


async def set_user_role(slack_user_id: str, role: str, granted_by: str = "") -> None:
    """Create or update a user's role."""
    sb = await _sb()
    await sb.table("user_permissions").upsert({
        "slack_user_id": slack_user_id,
        "role":          role,
        "granted_by":    granted_by,
    }).execute()


async def is_admin(slack_user_id: str) -> bool:
    """Return True if the user has admin role."""
    return await get_user_role(slack_user_id) == "admin"


async def list_user_permissions() -> list[dict]:
    """Return all rows from user_permissions."""
    sb = await _sb()
    result = await sb.table("user_permissions").select(
        "slack_user_id,role,granted_by,created_at"
    ).order("created_at").execute()
    return result.data or []


# ---------------------------------------------------------------------------
# Dynamic configurations
# ---------------------------------------------------------------------------

async def get_config(key: str) -> str | None:
    """Return the stored value for a config key, or None if absent."""
    try:
        sb = await _sb()
        result = await sb.table("dynamic_configs").select("value").eq("key", key).execute()
        if result.data:
            return result.data[0]["value"]
    except Exception as exc:
        logger.warning("get_config failed: %s", exc)
    return None


async def set_config(key: str, value: str, created_by: str = "") -> None:
    """Create or update a config entry."""
    sb = await _sb()
    await sb.table("dynamic_configs").upsert({
        "key":        key,
        "value":      value,
        "created_by": created_by,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }).execute()


async def list_configs() -> list[dict]:
    """Return all config entries ordered by key."""
    sb = await _sb()
    result = await sb.table("dynamic_configs").select(
        "key,value,created_by,updated_at"
    ).order("key").execute()
    return result.data or []


async def delete_config(key: str) -> bool:
    """Delete a config entry. Returns True if it existed."""
    sb = await _sb()
    result = await sb.table("dynamic_configs").select("key").eq("key", key).execute()
    if not result.data:
        return False
    await sb.table("dynamic_configs").delete().eq("key", key).execute()
    return True
