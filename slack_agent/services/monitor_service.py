"""
Delta monitoring service.

Each monitoring job runs at a configured interval and compares the current
client state against the last saved snapshot. Only posts to Slack when
Claude detects a meaningful change (urgency 🔴 or 🟠, configurable).

Snapshot format (JSON stored in monitoring_snapshots.context):
{
  "external_slack": str,   # last N messages from external workspace
  "readai":         str,   # latest Read.ai meeting info
  "hubspot":        str,   # latest HubSpot timeline entry
  "captured_at":    float  # unix timestamp
}
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

_DELTA_PROMPT = """\
Você é o monitor de clientes da equipe Tropical.
Compare os dois snapshots abaixo e identifique o que mudou.

SNAPSHOT ANTERIOR (capturado em {old_time}):
{old_context}

─────────────────────────────────────────
SNAPSHOT ATUAL (agora):
{new_context}
─────────────────────────────────────────

## CLASSIFIQUE a urgência da MUDANÇA (não do estado geral):
🔴 red    — problema crítico NOVO: bloqueio, erro sistêmico, reclamação grave sem resolução
🟠 orange — combinado NOVO pendente, insumo aguardado, prazo próximo, pergunta nova sem resposta
🟡 yellow — nova atividade sem urgência: reunião realizada, update de status, alinhamento ok
⚪ none   — sem mudança relevante no período

## FORMATO OBRIGATÓRIO DA RESPOSTA:
URGENCIA: <red|orange|yellow|none>
ALERTA: <2-3 linhas em markdown Slack descrevendo o que mudou e quem deve agir>

Se URGENCIA for yellow ou none, escreva "ALERTA: -" (não gere texto de alerta).
Nunca invente — só o que está nos dados.
"""


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

async def _fetch_client_snapshot(client_key: str, hours_back: int) -> dict:
    """Fetch a compact state for a client — used only for delta comparison."""
    from .report_service import _fetch_external_slack, _fetch_readai, _fetch_hubspot

    external, readai, hubspot = await asyncio.gather(
        _fetch_external_slack(client_key, hours_back),
        _fetch_readai(client_key, hours_back),
        _fetch_hubspot(client_key),
        return_exceptions=True,
    )

    def _s(v, limit: int) -> str:
        return str(v)[:limit] if not isinstance(v, Exception) else ""

    return {
        "external_slack": _s(external, 1000),
        "readai":         _s(readai, 600),
        "hubspot":        _s(hubspot, 600),
        "captured_at":    time.time(),
    }


def _snapshot_hash(snap: dict) -> str:
    content = snap.get("external_slack", "") + snap.get("readai", "") + snap.get("hubspot", "")
    return hashlib.md5(content.encode()).hexdigest()


def _format_snapshot(snap: dict) -> str:
    return (
        f"[SLACK EXTERNO]\n{snap.get('external_slack', '(vazio)')}\n\n"
        f"[READ.AI]\n{snap.get('readai', '(vazio)')}\n\n"
        f"[HUBSPOT]\n{snap.get('hubspot', '(vazio)')}"
    )


# ---------------------------------------------------------------------------
# Delta analysis
# ---------------------------------------------------------------------------

async def _ask_claude_delta(old_snap: dict, new_snap: dict) -> tuple[str, str | None]:
    """
    Returns (urgency, alert_text).
    urgency: "red" | "orange" | "yellow" | "none"
    alert_text: formatted Slack markdown or None
    """
    old_time = datetime.fromtimestamp(
        old_snap.get("captured_at", 0), tz=timezone.utc
    ).strftime("%d/%m %H:%M")

    prompt = _DELTA_PROMPT.format(
        old_time=old_time,
        old_context=_format_snapshot(old_snap)[:2500],
        new_context=_format_snapshot(new_snap)[:2500],
    )

    try:
        ai = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = await ai.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
    except Exception as exc:
        logger.error("Delta Claude call failed: %s", exc)
        return "none", None

    urgency = "none"
    alert_lines: list[str] = []
    in_alert = False

    for line in text.splitlines():
        if line.startswith("URGENCIA:"):
            urgency = line.split(":", 1)[1].strip().lower()
        elif line.startswith("ALERTA:"):
            val = line.split(":", 1)[1].strip()
            if val != "-":
                alert_lines.append(val)
            in_alert = True
        elif in_alert and line.strip():
            alert_lines.append(line.strip())

    alert = "\n".join(alert_lines).strip() or None
    if urgency in ("yellow", "none"):
        alert = None

    return urgency, alert


# ---------------------------------------------------------------------------
# Per-client delta check
# ---------------------------------------------------------------------------

async def run_delta_check(client_key: str, hours_back: int = 2) -> dict:
    """
    Compare current state vs last snapshot.
    Saves the new snapshot regardless.
    Returns: {"urgency": str, "alert": str|None}
    """
    from ..models.database import get_monitoring_snapshot, save_monitoring_snapshot

    new_snap = await _fetch_client_snapshot(client_key, hours_back)

    old_row = await get_monitoring_snapshot(client_key)
    if not old_row:
        # First run — baseline, no alert
        await save_monitoring_snapshot(client_key, json.dumps(new_snap), new_snap["captured_at"])
        logger.info("Monitor baseline saved for %s", client_key)
        return {"urgency": "none", "alert": None}

    old_snap = json.loads(old_row["context"])

    if _snapshot_hash(old_snap) == _snapshot_hash(new_snap):
        logger.debug("Monitor: no change for %s", client_key)
        # Still update captured_at so we don't drift
        new_snap["captured_at"] = old_snap.get("captured_at", new_snap["captured_at"])
        await save_monitoring_snapshot(client_key, json.dumps(new_snap), new_snap["captured_at"])
        return {"urgency": "none", "alert": None}

    logger.info("Monitor: change detected for %s — asking Claude", client_key)
    urgency, alert = await _ask_claude_delta(old_snap, new_snap)
    await save_monitoring_snapshot(client_key, json.dumps(new_snap), new_snap["captured_at"])
    return {"urgency": urgency, "alert": alert}


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

_PRIORITY_RANK = {"red": 3, "orange": 2, "yellow": 1, "none": 0}


async def run_all_monitor_jobs(slack_client) -> None:
    """
    Called by the scheduler every minute.
    Checks which monitoring jobs are due, runs delta checks, posts alerts.
    """
    from ..models.database import list_monitoring_jobs, update_monitoring_job_last_run
    from ..tools.slack_tools import _get_workspaces
    from datetime import datetime as dt2

    try:
        jobs = await list_monitoring_jobs()
    except Exception as exc:
        logger.error("run_all_monitor_jobs: failed to load jobs: %s", exc)
        return

    now_utc = datetime.now(tz=timezone.utc)

    for job in jobs:
        interval_sec = job["interval_minutes"] * 60
        last_run = job.get("last_run_at")

        if last_run:
            try:
                last_dt = dt2.fromisoformat(last_run.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < interval_sec:
                    continue  # not due yet
            except Exception:
                pass  # if we can't parse, run it

        # Determine client list
        client = job.get("client")
        if client:
            clients = [client]
        else:
            try:
                ws = await _get_workspaces()
                clients = list(ws.keys())
            except Exception as exc:
                logger.error("Monitor job %d: failed to get workspaces: %s", job["id"], exc)
                await update_monitoring_job_last_run(job["id"])
                continue

        # hours_back = slightly more than the interval so we don't miss messages
        hours_back = max(1, round(job["interval_minutes"] / 60 + 0.5))
        min_priority = job.get("min_priority", "orange")
        min_rank = _PRIORITY_RANK.get(min_priority, 2)

        # Run delta checks in parallel (cap concurrency)
        sem = asyncio.Semaphore(5)

        async def _check(ck: str) -> tuple[str, str | None, str]:
            async with sem:
                try:
                    res = await run_delta_check(ck, hours_back=hours_back)
                    return ck, res["urgency"], res["alert"]
                except Exception as exc:
                    logger.error("Delta check for %s failed: %s", ck, exc)
                    return ck, "none", None

        results = await asyncio.gather(*[_check(ck) for ck in clients])

        # Collect alerts meeting the min_priority threshold
        alerts: list[tuple[str, str, str]] = []  # (urgency, client_key, alert_text)
        for ck, urgency, alert in results:
            rank = _PRIORITY_RANK.get(urgency, 0)
            if rank >= min_rank and alert:
                alerts.append((urgency, ck, alert))

        if alerts:
            # Sort: red first, then orange
            alerts.sort(key=lambda x: _PRIORITY_RANK.get(x[0], 0), reverse=True)
            now_str = datetime.now().strftime("%d/%m %H:%M")
            lines = [f"🔔 *Monitor de mudanças — {now_str}*\n"]
            for urgency, ck, alert in alerts:
                emoji = "🔴" if urgency == "red" else "🟠"
                lines.append(f"{emoji} *{ck}*\n{alert}")
            msg = "\n\n".join(lines)
            try:
                await slack_client.chat_postMessage(channel=job["channel"], text=msg)
                logger.info(
                    "Monitor job %d: posted %d alert(s) to %s",
                    job["id"], len(alerts), job["channel"],
                )
            except Exception as exc:
                logger.error("Monitor job %d: post failed: %s", job["id"], exc)

        await update_monitoring_job_last_run(job["id"])
