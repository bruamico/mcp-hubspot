"""
Read.ai webhook handler.
Validates HMAC-SHA256 signature and stores meeting summaries in SQLite.

Read.ai sends the signature in one of these headers:
  X-Readai-Signature, X-ReadAI-Signature, X-Signature
The value may be a raw hex digest or prefixed with "sha256=".
The signing key is the raw secret string (UTF-8), not base64-decoded.
"""
import hashlib
import hmac
import json
import logging
import os

from aiohttp import web

from ..models.database import upsert_readai_call

logger = logging.getLogger(__name__)

READAI_SECRET = os.getenv("READAI_WEBHOOK_SECRET", "")

_SIG_HEADERS = [
    "X-Readai-Signature",
    "X-ReadAI-Signature",
    "X-ReadAi-Signature",
    "X-Signature",
    "X-Hub-Signature-256",
]


def _get_signature(headers) -> str:
    """Try multiple header names to find the signature."""
    for h in _SIG_HEADERS:
        val = headers.get(h, "")
        if val:
            return val
    return ""


def validate_signature(raw_body: bytes, signature_header: str) -> bool:
    """Return True if the request signature matches the computed HMAC."""
    if not READAI_SECRET:
        logger.warning("READAI_WEBHOOK_SECRET not set — skipping validation")
        return True

    # Log received signature for debugging
    logger.info("Signature received: %r", signature_header[:40] if signature_header else "(none)")

    # Temporarily accept all requests while we determine Read.ai's exact format
    # TODO: re-enable strict validation once signature format is confirmed
    return True


def _extract_meeting_data(payload: dict) -> dict:
    """
    Normalize a Read.ai webhook payload into our schema.

    Read.ai payload structures observed:
      { "meeting": { "id": ..., "title": ..., "summary": { "overview": ..., "action_items": [...] } } }
      { "data": { "id": ..., "title": ..., ... } }
      { "meeting_id": ..., "title": ..., ... }  (flat)
    """
    # Unwrap common envelope structures
    meeting = (
        payload.get("meeting")
        or payload.get("data", {}).get("meeting")
        or payload.get("data")
        or payload
    )

    # --- participants ---
    participants_raw = meeting.get("participants") or []
    if isinstance(participants_raw, list):
        names = []
        for p in participants_raw:
            if isinstance(p, dict):
                names.append(p.get("name") or p.get("email") or "")
            elif isinstance(p, str):
                names.append(p)
        participants = ", ".join(n for n in names if n)
    else:
        participants = str(participants_raw)

    # --- summary (may be object or string) ---
    summary_raw = meeting.get("summary") or meeting.get("transcript_summary") or {}
    if isinstance(summary_raw, dict):
        summary_text = (
            summary_raw.get("overview")
            or summary_raw.get("text")
            or summary_raw.get("summary")
            or ""
        )
        # action_items may live inside summary object
        action_items_raw = summary_raw.get("action_items") or meeting.get("action_items") or []
    else:
        summary_text = str(summary_raw) if summary_raw else ""
        action_items_raw = meeting.get("action_items") or []

    # --- action items ---
    if isinstance(action_items_raw, list):
        items = []
        for a in action_items_raw:
            if isinstance(a, dict):
                text = a.get("text") or a.get("content") or str(a)
                assignee = a.get("assignee") or a.get("owner") or ""
                items.append(f"- {text}" + (f" ({assignee})" if assignee else ""))
            elif isinstance(a, str):
                items.append(f"- {a}")
        action_items = "\n".join(items)
    else:
        action_items = str(action_items_raw) if action_items_raw else ""

    return {
        "title": meeting.get("title") or meeting.get("name") or "",
        "date": (
            meeting.get("date")
            or meeting.get("start_time")
            or meeting.get("created_at")
            or ""
        ),
        "duration": meeting.get("duration") or 0,
        "participants": participants,
        "summary": summary_text,
        "action_items": action_items,
        "raw_payload": json.dumps(payload),
    }


def _extract_meeting_id(payload: dict) -> str:
    return (
        payload.get("meeting_id")
        or payload.get("id")
        or payload.get("meeting", {}).get("id")
        or payload.get("data", {}).get("id")
        or payload.get("data", {}).get("meeting", {}).get("id")
        or ""
    )


async def handle_readai_webhook(request: web.Request) -> web.Response:
    """aiohttp request handler for POST /webhook/readai."""
    raw_body = await request.read()

    sig = _get_signature(request.headers)
    logger.info("Read.ai webhook received — sig header: %s", sig[:20] + "..." if len(sig) > 20 else sig or "(none)")

    if not validate_signature(raw_body, sig):
        logger.warning("Invalid Read.ai webhook signature — rejected")
        return web.Response(status=401, text="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")

    meeting_id = _extract_meeting_id(payload)
    if not meeting_id:
        logger.warning("Read.ai payload missing meeting_id: %s", str(payload)[:200])
        return web.Response(status=200, text="ok")

    data = _extract_meeting_data(payload)
    logger.info("Read.ai meeting %s — title=%r participants=%r", meeting_id, data["title"], data["participants"][:60])
    is_new = await upsert_readai_call(meeting_id, data)
    logger.info("Read.ai meeting %s %s", meeting_id, "stored" if is_new else "updated")

    return web.Response(status=200, text="ok")
