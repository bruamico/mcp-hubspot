"""
Read.ai webhook handler.
Validates HMAC-SHA256 signature (key = Base64-decoded secret) and stores
meeting summaries in SQLite.
"""
import base64
import hashlib
import hmac
import json
import logging
import os

from aiohttp import web

from ..models.database import upsert_readai_call

logger = logging.getLogger(__name__)

READAI_SECRET_B64 = os.getenv("READAI_WEBHOOK_SECRET", "")


def _get_hmac_key() -> bytes:
    """Decode the Base64-encoded secret into raw bytes."""
    return base64.b64decode(READAI_SECRET_B64)


def validate_signature(raw_body: bytes, signature_header: str) -> bool:
    """Return True if the request signature matches the computed HMAC."""
    if not READAI_SECRET_B64:
        logger.warning("READAI_WEBHOOK_SECRET not set — skipping validation")
        return True
    try:
        key = _get_hmac_key()
        computed = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        # Header may be prefixed with "sha256=" — strip it
        provided = signature_header.replace("sha256=", "").strip()
        return hmac.compare_digest(computed, provided)
    except Exception as exc:
        logger.error("Signature validation error: %s", exc)
        return False


def _extract_meeting_data(payload: dict) -> dict:
    """Normalize a Read.ai webhook payload into our schema."""
    meeting = payload.get("meeting") or payload
    participants = meeting.get("participants") or []
    if isinstance(participants, list):
        participants = ", ".join(
            p.get("name") or p.get("email", "") for p in participants
        )

    action_items = meeting.get("action_items") or []
    if isinstance(action_items, list):
        action_items = "\n".join(
            f"- {a.get('text', str(a))}" for a in action_items
        )

    return {
        "title": meeting.get("title") or meeting.get("name", ""),
        "date": meeting.get("date") or meeting.get("start_time", ""),
        "duration": meeting.get("duration") or 0,
        "participants": participants,
        "summary": meeting.get("summary") or meeting.get("transcript_summary", ""),
        "action_items": action_items,
        "raw_payload": json.dumps(payload),
    }


async def handle_readai_webhook(request: web.Request) -> web.Response:
    """aiohttp request handler for POST /webhook/readai."""
    raw_body = await request.read()

    sig = request.headers.get("X-ReadAI-Signature") or request.headers.get(
        "X-Signature", ""
    )
    if not validate_signature(raw_body, sig):
        logger.warning("Invalid Read.ai webhook signature — rejected")
        return web.Response(status=401, text="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")

    meeting_id = (
        payload.get("meeting_id")
        or payload.get("id")
        or payload.get("meeting", {}).get("id", "")
    )
    if not meeting_id:
        logger.warning("Read.ai payload missing meeting_id — ignoring")
        return web.Response(status=200, text="ok")

    data = _extract_meeting_data(payload)
    is_new = await upsert_readai_call(meeting_id, data)
    logger.info(
        "Read.ai meeting %s %s", meeting_id, "stored" if is_new else "already exists"
    )

    return web.Response(status=200, text="ok")
