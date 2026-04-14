"""
Automatic commitment extractor.

Every hour, reads new Slack messages from all client channels (external)
and the internal Tropical Hub, sends them to Claude, and extracts:
  - Pending commitments  → saved as commitment status='pending'
  - Fulfilled deliveries → matched against pending, marked status='done'
"""
import asyncio
import json
import logging
import os
import time

import anthropic

from ..models.database import (
    add_commitment, update_commitment, list_commitments,
    commitment_source_exists, get_watermark, set_watermark,
)

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """Você é um extrator de compromissos de conversas de Slack.
Analise as mensagens abaixo e extraia APENAS itens concretos e acionáveis.

Retorne um JSON com esta estrutura exata:
{
  "commitments": [
    {
      "type": "request",
      "description": "descrição objetiva do que foi pedido/combinado",
      "requested_by": "nome de quem pediu (ou vazio)",
      "assigned_to": "nome/@ de quem vai fazer (ou vazio)",
      "priority": "normal",
      "message_ts": "timestamp exato da mensagem no Slack"
    }
  ],
  "fulfillments": [
    {
      "description": "o que foi entregue",
      "message_ts": "timestamp da mensagem de entrega",
      "matches_description": "texto do pedido original que isso resolve (para cruzar com pendentes)"
    }
  ]
}

REGRAS:
- type="request": cliente pediu algo OU equipe se comprometeu com algo
  Exemplos: "pode me enviar X?", "vou mandar até sexta", "precisamos de Y", "ficou de fazer Z"
- type="request" com priority="high": prazo mencionado em menos de 2 dias, ou urgente
- type="request" com priority="critical": bloqueio de operação, erro em produção, cliente travado
- fulfillment: entrega clara foi feita — "aqui está X", "enviado", "feito", link/arquivo anexado
- NÃO extraia: perguntas casuais, atualizações de status sem ação, saudações, confirmações triviais
- Se não houver nada acionável, retorne {"commitments": [], "fulfillments": []}
- Responda SOMENTE com o JSON, sem texto adicional."""


# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------

async def _call_claude_extract(messages_text: str, client_key: str, channel_name: str) -> dict:
    """Call Claude to extract commitments from a block of messages."""
    if not messages_text.strip():
        return {"commitments": [], "fulfillments": []}
    try:
        ac = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = await ac.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=_EXTRACT_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Cliente: {client_key} | Canal: {channel_name}\n\n"
                    f"MENSAGENS:\n{messages_text}"
                ),
            }],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Claude extraction JSON parse error for %s/%s: %s", client_key, channel_name, exc)
        return {"commitments": [], "fulfillments": []}
    except Exception as exc:
        logger.error("Claude extraction failed for %s/%s: %s", client_key, channel_name, exc)
        return {"commitments": [], "fulfillments": []}


async def _process_channel(
    sc,
    channel_id: str,
    channel_name: str,
    client_key: str,
    workspace_label: str,
) -> tuple[int, int]:
    """
    Fetch new messages for one channel, extract commitments, save results.
    Returns (new_commitments, resolved_commitments).
    """
    # Get watermark — only process messages newer than last run
    last_ts = await get_watermark(channel_id, workspace_label)
    oldest = last_ts if last_ts else str(time.time() - 7 * 24 * 3600)  # default: 7 days back

    try:
        resp = await sc.conversations_history(
            channel=channel_id,
            oldest=oldest,
            limit=100,
            inclusive=False,
        )
    except Exception as exc:
        logger.warning("conversations_history failed for %s/%s: %s", workspace_label, channel_name, exc)
        return 0, 0

    msgs = [
        m for m in resp.get("messages", [])
        if not m.get("subtype") and m.get("text", "").strip()
    ]
    if not msgs:
        return 0, 0

    # Resolve user IDs to display names so Claude sees real names, not @UXXX
    # Build fallback client list: workspace token first, then Tropical Hub token.
    # This handles the common case where a Tropical Hub member is mentioned in an
    # external workspace channel — the external token can't resolve Tropical Hub UIDs,
    # but TROPICAL_BOT_TOKEN can.
    _user_cache: dict[str, str] = {}
    _tropical_token = os.getenv("TROPICAL_BOT_TOKEN", "")
    from slack_sdk.web.async_client import AsyncWebClient as _AWC
    # Build fallback list; avoid duplicate if both tokens are the same
    _ws_token = (sc.token if hasattr(sc, "token") else "")
    _fallback_clients = [sc] + (
        [_AWC(token=_tropical_token)] if _tropical_token and _tropical_token != _ws_token else []
    )

    async def _resolve_user(uid: str) -> str:
        if uid not in _user_cache:
            for _client in _fallback_clients:
                try:
                    info = await _client.users_info(user=uid)
                    u = info.get("user", {})
                    name = (
                        u.get("profile", {}).get("display_name")
                        or u.get("real_name")
                        or u.get("name")
                        or ""
                    )
                    if name:
                        _user_cache[uid] = name
                        break
                except Exception:
                    continue
            else:
                _user_cache[uid] = uid  # couldn't resolve with any token
        return _user_cache[uid]

    import re as _re

    async def _resolve_text(text: str) -> str:
        for uid in set(_re.findall(r"<@([A-Z0-9]+)>", text)):
            name = await _resolve_user(uid)
            text = text.replace(f"<@{uid}>", f"@{name}")
        return text

    # Build text block for Claude with resolved names
    lines = []
    for m in reversed(msgs):
        ts = m.get("ts", "")
        uid = m.get("user", "")
        sender = (await _resolve_user(uid)) if uid else m.get("username", "?")
        raw_text = m.get("text", "").replace("\n", " ")[:300]
        text = await _resolve_text(raw_text)
        lines.append(f"[{ts}] {sender}: {text}")
    messages_text = "\n".join(lines)

    # Update watermark to highest ts seen
    newest_ts = max(m.get("ts", "0") for m in msgs)

    extracted = await _call_claude_extract(messages_text, client_key, channel_name)

    new_count = 0
    resolved_count = 0

    # Save new commitments (dedup by source_channel + source_ts)
    for c in extracted.get("commitments", []):
        ts = c.get("message_ts", "")
        source_ch = f"{workspace_label}#{channel_name}"

        if ts and await commitment_source_exists(source_ch, ts):
            continue  # already extracted from this exact message

        # Use message timestamp as created_at so age reflects the original Slack date
        msg_created_at = None
        if ts:
            try:
                import datetime as _dt
                msg_created_at = _dt.datetime.fromtimestamp(
                    float(ts), tz=_dt.timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        cid = await add_commitment(
            client_key=client_key,
            description=c.get("description", "")[:500],
            requested_by=c.get("requested_by", ""),
            assigned_to=c.get("assigned_to", ""),
            priority=c.get("priority", "normal"),
            source_channel=source_ch,
            source_ts=ts,
            msg_created_at=msg_created_at,
        )
        logger.info("Auto-extracted commitment #%d for %s: %s", cid, client_key, c.get("description", "")[:80])
        new_count += 1

    # Match fulfillments against pending commitments
    if extracted.get("fulfillments"):
        pending = await list_commitments(client_key=client_key, status="pending", limit=50)
        for f in extracted["fulfillments"]:
            match_text = (f.get("matches_description") or "").lower()
            if not match_text:
                continue
            for p in pending:
                if _text_overlap(match_text, p["description"].lower()) > 0.4:
                    await update_commitment(
                        p["id"],
                        status="done",
                        notes=f"Detectado automaticamente: {f.get('description', '')}",
                    )
                    logger.info(
                        "Auto-resolved commitment #%d for %s: %s",
                        p["id"], client_key, p["description"][:60],
                    )
                    resolved_count += 1
                    break  # one fulfillment resolves one commitment

    await set_watermark(channel_id, workspace_label, newest_ts)
    return new_count, resolved_count


def _text_overlap(a: str, b: str) -> float:
    """Simple word-overlap ratio between two strings (Jaccard similarity)."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_extraction(slack_client=None) -> dict:
    """
    Run a full extraction cycle over all external client workspaces.
    Also scans internal Tropical Hub channels.
    Returns summary dict with counts.
    """
    from ..tools.slack_tools import _get_workspaces
    from ..tools.slack_tools import _get_tropical_channels, _paginated_channels
    from slack_sdk.web.async_client import AsyncWebClient

    total_new = 0
    total_resolved = 0
    errors = 0

    # ── External client workspaces ──────────────────────────────────────────
    workspaces = await _get_workspaces()
    sem = asyncio.Semaphore(4)  # max 4 workspaces in parallel

    async def _process_workspace(client_key: str, entry: dict) -> None:
        nonlocal total_new, total_resolved, errors
        token = entry.get("token", "")
        if not token:
            return
        sc = AsyncWebClient(token=token)
        try:
            channels = await _paginated_channels(sc)
        except Exception as exc:
            logger.warning("Could not list channels for %s: %s", client_key, exc)
            errors += 1
            return

        for ch in channels:
            if ch.get("is_archived"):
                continue
            async with sem:
                try:
                    n, r = await _process_channel(
                        sc, ch["id"], ch["name"], client_key, f"ext:{client_key}"
                    )
                    total_new += n
                    total_resolved += r
                except Exception as exc:
                    logger.warning("Error processing %s#%s: %s", client_key, ch["name"], exc)
                    errors += 1

    await asyncio.gather(*[_process_workspace(k, v) for k, v in workspaces.items()])

    # ── Internal Tropical Hub channels ─────────────────────────────────────
    tropical_token = os.getenv("TROPICAL_BOT_TOKEN", "")
    if tropical_token:
        sc_internal = AsyncWebClient(token=tropical_token)
        try:
            int_channels = await _get_tropical_channels(sc_internal)
            _SKIP = {
                "geral", "general", "random", "aleatorio", "equipe", "time", "team",
                "dev", "developers", "bot-alertas", "bot-testes", "bot-logs",
                "announcements", "marketing", "vendas", "financeiro", "rh",
                "people", "ops", "operacoes", "internal", "bots",
            }
            for ch in int_channels:
                if ch.get("is_archived") or ch["name"] in _SKIP:
                    continue
                # Channel name is the client key for internal channels
                client_key = ch["name"]
                async with sem:
                    try:
                        n, r = await _process_channel(
                            sc_internal, ch["id"], ch["name"], client_key, "internal"
                        )
                        total_new += n
                        total_resolved += r
                    except Exception as exc:
                        logger.warning("Error processing internal#%s: %s", ch["name"], exc)
                        errors += 1
        except Exception as exc:
            logger.error("Internal Slack extraction failed: %s", exc)
            errors += 1

    summary = {
        "new_commitments": total_new,
        "resolved_commitments": total_resolved,
        "errors": errors,
    }
    logger.info("Extraction complete: %s", summary)
    return summary
