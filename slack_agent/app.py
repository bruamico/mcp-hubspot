"""
Slack Agent — main entry point.

Runs two concurrent servers:
  1. Slack Bolt (Socket Mode) — handles @mentions and DMs
  2. aiohttp HTTP server on PORT_WEBHOOK — handles Read.ai webhooks

Environment variables required:
  TROPICAL_BOT_TOKEN       xoxb-...
  TROPICAL_APP_TOKEN       xapp-...
  ANTHROPIC_API_KEY        sk-ant-...
  HUBSPOT_ACCESS_TOKEN     pat-...   (or HUBSPOT_TOKEN)
  REDIS_URL                rediss://...
  READAI_WEBHOOK_SECRET    base64-encoded secret
  SLACK_REPORT_CHANNEL     #channel for proactive alerts (default: #geral)
  DB_PATH                  SQLite path (default: /app/data/tropical_bot.db)
  PORT_WEBHOOK             Port for webhook server (default: 8080)
"""
import asyncio
import logging
import os
import re

from aiohttp import web
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from .agent import run_agent
from .models.database import init_db, save_oauth_token, reprocess_raw_payloads
from .prompts import SYSTEM_PROMPT
from .services.conversation import ConversationMemory
from .services.report_service import (
    is_report_request, generate_report, extract_hours_back, extract_client,
)
from .services.webhook import handle_readai_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Slack app
# ---------------------------------------------------------------------------
slack_app = AsyncApp(token=os.environ["TROPICAL_BOT_TOKEN"])
memory = ConversationMemory(os.environ["REDIS_URL"])

BOT_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def _strip_mention(text: str) -> str:
    return BOT_MENTION_RE.sub("", text).strip()


async def _process_message(event: dict, say, client) -> None:
    """Shared handler for mentions and direct messages."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    raw_text = event.get("text") or ""
    user_message = _strip_mention(raw_text)

    if not user_message:
        return

    # Show "thinking" reaction
    try:
        await client.reactions_add(
            channel=channel, timestamp=event["ts"], name="thinking_face"
        )
    except Exception:
        pass  # Reaction failure is non-critical

    try:
        history = await memory.get_history(channel, thread_ts)

        # Fast path only when NOT asking to filter by calendar/agenda
        _calendar_keywords = ("agenda", "reunião de hoje", "meetings", "calendário")
        _wants_calendar_filter = any(kw in user_message.lower() for kw in _calendar_keywords)

        if is_report_request(user_message) and not _wants_calendar_filter:
            # Fast path: pre-fetch all data in parallel, single Claude synthesis call
            from .tools.slack_tools import _get_workspaces, _get_tropical_channels
            from slack_sdk.web.async_client import AsyncWebClient as _ASC

            ws = await _get_workspaces()

            # Supplement WORKSPACES_JSON with internal Slack channel names so that
            # clients without an external workspace (e.g. "ativa") still appear.
            # We skip generic/internal channels that are not client names.
            _SKIP = {
                "geral", "general", "random", "aleatorio", "equipe", "time", "team",
                "dev", "developers", "bot-alertas", "bot-testes", "bot-logs",
                "announcements", "marketing", "vendas", "financeiro", "rh", "people",
                "ops", "operacoes", "internal", "bots",
            }
            try:
                _sc = _ASC(token=os.getenv("TROPICAL_BOT_TOKEN", ""))
                _channels = await _get_tropical_channels(_sc)
                _ch_names = [
                    ch["name"] for ch in _channels
                    if ch["name"] not in _SKIP and not ch["name"].startswith("_")
                ]
            except Exception:
                _ch_names = []

            # Preserve WORKSPACES_JSON order, append any channel-only clients at end
            all_clients = list(dict.fromkeys(list(ws.keys()) + _ch_names))
            # CLIENT_LIST env var can further extend the list if needed
            _extra = [c.strip() for c in os.getenv("CLIENT_LIST", "").split(",") if c.strip()]
            all_clients = list(dict.fromkeys(all_clients + _extra))

            specific = extract_client(user_message, all_clients)
            clients = [specific] if specific else all_clients
            hours_back = extract_hours_back(user_message, default=48)

            response = await generate_report(clients, hours_back=hours_back)
        else:
            response = await run_agent(
                user_message=user_message,
                history=history,
                system_prompt=SYSTEM_PROMPT,
            )

        await memory.add_message(channel, thread_ts, "user", user_message)
        await memory.add_message(channel, thread_ts, "assistant", response)

        await say(text=response, thread_ts=thread_ts)

    except Exception as exc:
        logger.error("Agent error: %s", exc, exc_info=True)
        await say(
            text=f":x: Ocorreu um erro ao processar sua mensagem: `{exc}`",
            thread_ts=thread_ts,
        )
    finally:
        try:
            await client.reactions_remove(
                channel=channel, timestamp=event["ts"], name="thinking_face"
            )
        except Exception:
            pass


@slack_app.event("app_mention")
async def handle_mention(event, say, client):
    await _process_message(event, say, client)


@slack_app.event("message")
async def handle_dm(event, say, client):
    # Only handle DMs (channel_type == "im") to avoid double-processing in public channels
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        await _process_message(event, say, client)


# ---------------------------------------------------------------------------
# aiohttp webhook server
# ---------------------------------------------------------------------------

async def build_webhook_app() -> web.Application:
    aio_app = web.Application()
    aio_app.router.add_post("/webhook/readai", handle_readai_webhook)
    aio_app.router.add_get("/health", lambda r: web.Response(text="ok"))
    aio_app.router.add_get("/status", _status_handler)
    aio_app.router.add_get("/oauth/granola", _granola_oauth_start)
    aio_app.router.add_get("/oauth/granola/callback", _granola_oauth_callback)
    aio_app.router.add_get("/oauth/google", _google_oauth_start)
    aio_app.router.add_get("/oauth/google/callback", _google_oauth_callback)
    return aio_app


async def _status_handler(request: web.Request) -> web.Response:
    """Returns a JSON status blob — useful for verifying the running instance."""
    import json as _json
    import subprocess as _sp
    try:
        git_sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        git_sha = "unknown"
    status = {
        "status": "ok",
        "git_sha": git_sha,
        "model": os.getenv("CLAUDE_MODEL", "?"),
        "db_path": os.getenv("DB_PATH", "?"),
        "report_channel": os.getenv("SLACK_REPORT_CHANNEL", "?"),
        "hub_token_set": bool(os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN")),
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "redis_url_set": bool(os.getenv("REDIS_URL")),
    }
    return web.Response(
        text=_json.dumps(status, indent=2),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Granola OAuth 2.0 + PKCE flow
# ---------------------------------------------------------------------------

import base64
import hashlib
import secrets as _secrets

_pkce_store: dict[str, str] = {}  # state → code_verifier (in-memory, short-lived)

_GRANOLA_CLIENT_ID = "client_01KNS9F4SQRJHEXA3HCH4N8VBQ"
_GRANOLA_AUTH_URL = "https://mcp-auth.granola.ai/oauth2/authorize"
_GRANOLA_TOKEN_URL = "https://mcp-auth.granola.ai/oauth2/token"
_GRANOLA_REDIRECT = "https://tropical-bot.fly.dev/oauth/granola/callback"
_GRANOLA_SCOPES = "openid email profile offline_access"


async def _granola_oauth_start(request: web.Request) -> web.Response:
    """Redirect user to Granola OAuth authorization page."""
    # Generate PKCE
    code_verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = _secrets.token_hex(16)
    _pkce_store[state] = code_verifier

    from urllib.parse import urlencode
    params = urlencode({
        "client_id": _GRANOLA_CLIENT_ID,
        "redirect_uri": _GRANOLA_REDIRECT,
        "response_type": "code",
        "scope": _GRANOLA_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    raise web.HTTPFound(f"{_GRANOLA_AUTH_URL}?{params}")


async def _granola_oauth_callback(request: web.Request) -> web.Response:
    """Handle OAuth callback, exchange code for tokens, save to DB."""
    import aiohttp as _aiohttp

    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    if error:
        return web.Response(
            text=f"OAuth error: {error}",
            content_type="text/html",
            status=400,
        )
    if not code or not state or state not in _pkce_store:
        return web.Response(text="Invalid callback parameters.", status=400)

    code_verifier = _pkce_store.pop(state)

    async with _aiohttp.ClientSession() as session:
        async with session.post(_GRANOLA_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _GRANOLA_REDIRECT,
            "client_id": _GRANOLA_CLIENT_ID,
            "code_verifier": code_verifier,
        }) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Granola token exchange failed: %s", body)
                return web.Response(text=f"Token exchange failed: {body}", status=500)
            data = await resp.json()

    await save_oauth_token(
        service="granola",
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_in=data.get("expires_in", 3600),
        scope=data.get("scope", ""),
    )
    logger.info("Granola OAuth tokens saved successfully")

    return web.Response(
        content_type="text/html",
        text="""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Granola autorizado</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#0f0f0f;color:#fff;}
.box{text-align:center;padding:2rem;border:1px solid #333;border-radius:12px;}
h1{color:#4ade80;} p{color:#aaa;}</style></head>
<body><div class="box">
<h1>✅ Granola autorizado!</h1>
<p>O Tropical Bot agora tem acesso às suas notas de reunião do Granola.</p>
<p>Pode fechar esta janela e voltar ao Slack.</p>
</div></body></html>""",
    )


# ---------------------------------------------------------------------------
# Google Calendar OAuth 2.0 flow
# ---------------------------------------------------------------------------

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REDIRECT = "https://tropical-bot.fly.dev/oauth/google/callback"
_GOOGLE_SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
])

_google_state_store: dict[str, str] = {}  # state → "pending"


async def _google_oauth_start(request: web.Request) -> web.Response:
    from urllib.parse import urlencode
    state = _secrets.token_hex(16)
    _google_state_store[state] = "pending"
    params = urlencode({
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": _GOOGLE_REDIRECT,
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    raise web.HTTPFound(f"{_GOOGLE_AUTH_URL}?{params}")


async def _google_oauth_callback(request: web.Request) -> web.Response:
    import aiohttp as _aiohttp

    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    if error:
        return web.Response(text=f"OAuth error: {error}", status=400)
    if not code or not state or state not in _google_state_store:
        return web.Response(text="Parâmetros inválidos.", status=400)

    _google_state_store.pop(state)

    async with _aiohttp.ClientSession() as session:
        async with session.post(_GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _GOOGLE_REDIRECT,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        }) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Google token exchange failed: %s", body)
                return web.Response(text=f"Token exchange failed: {body}", status=500)
            data = await resp.json()

    await save_oauth_token(
        service="google_calendar",
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_in=data.get("expires_in", 3600),
        scope=data.get("scope", ""),
    )
    logger.info("Google Calendar OAuth tokens saved successfully")

    return web.Response(
        content_type="text/html",
        text="""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Google Calendar autorizado</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
height:100vh;margin:0;background:#0f0f0f;color:#fff;}
.box{text-align:center;padding:2rem;border:1px solid #333;border-radius:12px;}
h1{color:#4ade80;} p{color:#aaa;}</style></head>
<body><div class="box">
<h1>✅ Google Calendar autorizado!</h1>
<p>O Tropical Bot agora tem acesso à sua agenda.</p>
<p>Pode fechar esta janela e voltar ao Slack.</p>
</div></body></html>""",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    # ── Step 1: Database ──────────────────────────────────────────────────
    logger.info("Step 1/5 — Initializing database at %s", os.getenv("DB_PATH", "/app/data/tropical_bot.db"))
    try:
        await init_db()
        n = await reprocess_raw_payloads()
        if n:
            logger.info("Re-processed %d Read.ai records with missing fields", n)
        logger.info("Database OK")
    except Exception as exc:
        logger.critical("Database init FAILED: %s", exc, exc_info=True)
        raise

    # ── Step 2: Slack Socket Mode handler ────────────────────────────────
    logger.info("Step 2/5 — Creating Slack Socket Mode handler")
    try:
        socket_handler = AsyncSocketModeHandler(
            slack_app, os.environ["TROPICAL_APP_TOKEN"]
        )
        logger.info("Socket Mode handler created OK")
    except Exception as exc:
        logger.critical("Socket Mode handler FAILED: %s", exc, exc_info=True)
        raise

    # ── Step 3: Scheduler ────────────────────────────────────────────────
    logger.info("Step 3/5 — Starting scheduler")
    try:
        from .models.database import was_alert_sent, mark_alert_sent
        from .services.scheduler import init_scheduler
        scheduler = init_scheduler(
            slack_client=slack_app.client,
            hubspot_client=_lazy_hubspot(),
            was_alert_sent_fn=was_alert_sent,
            mark_alert_sent_fn=mark_alert_sent,
        )
        scheduler.start()
        logger.info("Scheduler started OK (%d jobs)", len(scheduler.get_jobs()))
    except Exception as exc:
        logger.critical("Scheduler FAILED: %s", exc, exc_info=True)
        raise

    # ── Step 4: aiohttp webhook server ───────────────────────────────────
    logger.info("Step 4/5 — Starting webhook server")
    try:
        webhook_port = int(os.getenv("PORT_WEBHOOK", "8080"))
        webhook_app = await build_webhook_app()
        runner = web.AppRunner(webhook_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", webhook_port)
        await site.start()
        logger.info("Webhook server listening on port %d OK", webhook_port)
    except Exception as exc:
        logger.critical("Webhook server FAILED: %s", exc, exc_info=True)
        raise

    # ── Step 5: Connect to Slack ─────────────────────────────────────────
    logger.info("Step 5/5 — Connecting to Slack Socket Mode (this blocks until shutdown)")
    try:
        report_channel = os.getenv("SLACK_REPORT_CHANNEL", "#geral")
        await slack_app.client.chat_postMessage(
            channel=report_channel,
            text=":white_check_mark: *Tropical Bot reiniciado* — todas as integrações carregadas. Pronto para uso.",
        )
    except Exception:
        pass  # startup ping is best-effort; don't abort if it fails

    await socket_handler.start_async()


def _lazy_hubspot():
    """Return HubSpotDirectClient for scheduler use."""
    try:
        from .tools.hubspot_tools import HubSpotDirectClient
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN")
        if not token:
            return None
        return HubSpotDirectClient(access_token=token)
    except Exception as exc:
        logger.warning("HubSpot client unavailable for scheduler: %s", exc)
        return None


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
