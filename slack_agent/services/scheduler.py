"""
Proactive scheduler using APScheduler.
Runs background jobs that post alerts to Slack without user interaction.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Configurable via env
REPORT_CHANNEL = os.getenv("SLACK_REPORT_CHANNEL", "#geral")
SAO_PAULO_TZ = "America/Sao_Paulo"

# Injected at startup
_slack_client = None
_hubspot = None
_db_was_alert_sent = None
_db_mark_alert_sent = None


def init_scheduler(slack_client, hubspot_client, was_alert_sent_fn, mark_alert_sent_fn) -> AsyncIOScheduler:
    """
    Configure and return the scheduler.
    Call this once at app startup, then call scheduler.start().
    """
    global _slack_client, _hubspot, _db_was_alert_sent, _db_mark_alert_sent
    _slack_client = slack_client
    _hubspot = hubspot_client
    _db_was_alert_sent = was_alert_sent_fn
    _db_mark_alert_sent = mark_alert_sent_fn

    scheduler = AsyncIOScheduler(timezone=SAO_PAULO_TZ)

    # Daily HubSpot summary — 9:00 AM São Paulo
    scheduler.add_job(
        _daily_hubspot_summary,
        CronTrigger(hour=9, minute=0, timezone=SAO_PAULO_TZ),
        id="daily_summary",
        replace_existing=True,
    )

    # Deal follow-up check — every 4 hours
    scheduler.add_job(
        _check_deals_without_followup,
        CronTrigger(hour="8,12,16,20", minute=0, timezone=SAO_PAULO_TZ),
        id="deal_followup",
        replace_existing=True,
    )

    # Dynamic scheduled reports — check every minute, run those that are due
    scheduler.add_job(
        _run_due_scheduled_reports,
        "interval",
        minutes=1,
        id="dynamic_reports",
        replace_existing=True,
    )

    # Delta monitoring jobs — check every minute, run those that are due
    scheduler.add_job(
        _run_due_monitor_jobs,
        "interval",
        minutes=1,
        id="delta_monitors",
        replace_existing=True,
    )

    # Automatic commitment extraction — every 60 minutes
    scheduler.add_job(
        _run_commitment_extraction,
        "interval",
        minutes=60,
        id="commitment_extraction",
        replace_existing=True,
    )

    logger.info("Scheduler configured with %d jobs", len(scheduler.get_jobs()))
    return scheduler


async def _daily_hubspot_summary() -> None:
    """Post a morning briefing with active contacts, companies, open tickets and overdue commitments."""
    if not _slack_client or not _hubspot:
        return
    try:
        import asyncio
        contacts_raw = await asyncio.to_thread(_hubspot.get_recent_contacts, limit=5)
        companies_raw = await asyncio.to_thread(_hubspot.get_recent_companies, limit=5)

        now_br = datetime.now(tz=timezone(timedelta(hours=-3)))
        date_str = now_br.strftime("%d/%m/%Y")

        message = (
            f":sunrise: *Bom dia! Resumo HubSpot — {date_str}*\n\n"
            f"*Contatos recentes:*\n```{contacts_raw[:800]}```\n"
            f"*Empresas recentes:*\n```{companies_raw[:800]}```\n\n"
            f"_Para mais detalhes, me pergunte no canal!_"
        )

        await _slack_client.chat_postMessage(channel=REPORT_CHANNEL, text=message)
        logger.info("Daily summary posted to %s", REPORT_CHANNEL)
    except Exception as exc:
        logger.error("Daily summary failed: %s", exc)

    # Overdue commitments alert — separate message so it's always visible
    await _alert_overdue_commitments()


async def _alert_overdue_commitments() -> None:
    """Post an alert for commitments pending more than 3 days."""
    if not _slack_client:
        return
    try:
        from ..models.database import get_overdue_commitments
        rows = await get_overdue_commitments(days_old=3)
        if not rows:
            return

        # Group by client
        by_client: dict[str, list] = {}
        for r in rows:
            by_client.setdefault(r["client_key"], []).append(r)

        lines = [f":hourglass_flowing_sand: *{len(rows)} compromisso(s) pendente(s) há +3 dias:*\n"]
        for ck, items in by_client.items():
            lines.append(f"*{ck.upper()}*")
            for c in items:
                assigned = f" → {c['assigned_to']}" if c.get("assigned_to") else ""
                try:
                    from datetime import datetime as _dt2
                    created = _dt2.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        from datetime import timezone as _tz2
                        created = created.replace(tzinfo=_tz2.utc)
                    days = (datetime.now(tz=timezone.utc) - created).days
                    age = f" ({days}d)"
                except Exception:
                    age = ""
                lines.append(f"  • #{c['id']} {c['description']}{assigned}{age}")

        await _slack_client.chat_postMessage(
            channel=REPORT_CHANNEL,
            text="\n".join(lines),
        )
        logger.info("Overdue commitments alert posted (%d items)", len(rows))
    except Exception as exc:
        logger.error("_alert_overdue_commitments failed: %s", exc)


async def _run_due_scheduled_reports() -> None:
    """Check the scheduled_reports table and run any that are due."""
    if not _slack_client:
        return
    try:
        import aiosqlite
        from ..models.database import DB_PATH
        from ..agent import run_agent
        from ..prompts import SYSTEM_PROMPT

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, client, interval_minutes, hours_back, channel, last_run_at
                   FROM scheduled_reports WHERE active=1"""
            ) as cur:
                reports = [dict(r) for r in await cur.fetchall()]

        for report in reports:
            last_run = report["last_run_at"]
            interval_sec = report["interval_minutes"] * 60
            if last_run:
                from datetime import datetime as dt2
                last_dt = dt2.fromisoformat(last_run.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - last_dt).total_seconds()
                if elapsed < interval_sec:
                    continue

            # Build prompt for the report
            client = report["client"]
            hours_back = report["hours_back"]
            if client:
                prompt = f"Gere o relatório do cliente {client} das últimas {hours_back}h."
            else:
                prompt = f"Gere o relatório de todos os clientes das últimas {hours_back}h."

            try:
                response = await run_agent(
                    user_message=prompt,
                    history=[],
                    system_prompt=SYSTEM_PROMPT,
                )
                await _slack_client.chat_postMessage(
                    channel=report["channel"],
                    text=f"📊 *Relatório automático*\n\n{response}",
                )
                # Update last_run_at
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE scheduled_reports SET last_run_at=? WHERE id=?",
                        (now_iso, report["id"]),
                    )
                    await db.commit()
                logger.info("Scheduled report %d posted to %s", report["id"], report["channel"])
            except Exception as exc:
                logger.error("Scheduled report %d failed: %s", report["id"], exc)
    except Exception as exc:
        logger.error("_run_due_scheduled_reports failed: %s", exc)


async def _run_due_monitor_jobs() -> None:
    """Delegate to monitor_service — runs all due delta monitoring jobs."""
    if not _slack_client:
        return
    try:
        from .monitor_service import run_all_monitor_jobs
        await run_all_monitor_jobs(_slack_client)
    except Exception as exc:
        logger.error("_run_due_monitor_jobs failed: %s", exc)


async def _run_commitment_extraction() -> None:
    """Run automatic commitment extraction from all Slack channels."""
    try:
        from .commitment_extractor import run_extraction
        summary = await run_extraction()
        if summary["new_commitments"] or summary["resolved_commitments"]:
            logger.info(
                "Commitment extraction: +%d new, %d resolved, %d errors",
                summary["new_commitments"], summary["resolved_commitments"], summary["errors"],
            )
    except Exception as exc:
        logger.error("_run_commitment_extraction failed: %s", exc)


async def _check_deals_without_followup() -> None:
    """
    Check for contacts/companies with no recent activity and alert the team.
    Uses a dedup key so the same alert is not spammed every 4 hours.
    """
    if not _slack_client or not _hubspot or not _db_was_alert_sent:
        return
    try:
        import asyncio
        # Pull recent tickets with default criteria (open / recently modified)
        result = await asyncio.to_thread(_hubspot.get_tickets, criteria="default", limit=20)
        tickets = result.get("results", []) if isinstance(result, dict) else []

        stale = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=3)

        for ticket in tickets:
            props = ticket.get("properties", {})
            last_modified = props.get("hs_lastmodifieddate", "")
            ticket_name = props.get("subject") or props.get("hs_ticket_id", "?")
            if last_modified:
                try:
                    mod_dt = datetime.fromisoformat(
                        last_modified.replace("Z", "+00:00")
                    )
                    if mod_dt < cutoff:
                        stale.append(ticket_name)
                except ValueError:
                    pass

        if not stale:
            return

        alert_key = f"stale_tickets_{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}"
        if await _db_was_alert_sent(alert_key):
            return

        message = (
            f":warning: *Tickets sem atualização há mais de 3 dias ({len(stale)}):*\n"
            + "\n".join(f"• {t}" for t in stale[:10])
        )
        await _slack_client.chat_postMessage(channel=REPORT_CHANNEL, text=message)
        await _db_mark_alert_sent(alert_key, REPORT_CHANNEL, message)
        logger.info("Stale tickets alert posted (%d tickets)", len(stale))
    except Exception as exc:
        logger.error("Deal follow-up check failed: %s", exc)
