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

    # Strip common prefix
    provided = signature_header.replace("sha256=", "").strip()

    if not provided:
        logger.warning("No signature header received — rejecting")
        return False

    # Use the raw secret string as key (UTF-8 bytes)
    key = READAI_SECRET.encode("utf-8")
    computed_hex = hmac.new(key, raw_body, hashlib.sha256).hexdigest()

    # Compare hex vs hex
    if hmac.compare_digest(computed_hex, provided.lower()):
        return True

    # Some services send base64-encoded digest instead of hex
    import base64
    try:
        computed_b64 = base64.b64encode(
            bytes.fromhex(computed_hex)
        ).decode("utf-8").rstrip("=")
        provided_b64 = provided.rstrip("=")
        if hmac.compare_digest(computed_b64, provided_b64):
            return True
    except Exception:
        pass

    logger.warning(
        "Signature mismatch — computed hex: %s, provided: %s",
        computed_hex[:16] + "...", provided[:16] + "..."
    )
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

    sig = _get_signature(request.headers)
    logger.info("Read.ai webhook received — sig header: %s", sig[:20] + "..." if len(sig) > 20 else sig or "(none)")

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
