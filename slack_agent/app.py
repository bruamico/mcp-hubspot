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
from .models.database import init_db
from .prompts import SYSTEM_PROMPT
from .services.conversation import ConversationMemory
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
    return aio_app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("Initializing database…")
    await init_db()

    logger.info("Starting Slack Socket Mode handler…")
    socket_handler = AsyncSocketModeHandler(
        slack_app, os.environ["TROPICAL_APP_TOKEN"]
    )

    # Set up proactive scheduler
    from .models.database import was_alert_sent, mark_alert_sent
    from .services.scheduler import init_scheduler
    scheduler = init_scheduler(
        slack_client=slack_app.client,
        hubspot_client=_lazy_hubspot(),
        was_alert_sent_fn=was_alert_sent,
        mark_alert_sent_fn=mark_alert_sent,
    )
    scheduler.start()
    logger.info("Scheduler started")

    # aiohttp webhook server
    webhook_port = int(os.getenv("PORT_WEBHOOK", "8080"))
    webhook_app = await build_webhook_app()
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", webhook_port)
    await site.start()
    logger.info("Webhook server listening on port %d", webhook_port)

    # Start Slack socket mode (blocks)
    await socket_handler.start_async()


def _lazy_hubspot():
    """Return HubSpotClient lazily to avoid import at startup."""
    try:
        from mcp_server_hubspot.hubspot_client import HubSpotClient
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN")
        return HubSpotClient(access_token=token)
    except Exception as exc:
        logger.warning("HubSpot client unavailable for scheduler: %s", exc)
        return None


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
