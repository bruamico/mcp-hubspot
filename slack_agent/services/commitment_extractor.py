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


# ---------------------------------------------------------------------------
# Supabase meetings → commitments + auto-memory
# ---------------------------------------------------------------------------

async def _identify_client_from_supabase_meeting(meeting: dict) -> str:
    """
    Best-effort client identification from a Supabase meeting row.

    Strategy (in order of confidence):
    1. Workspace key appears in title, summary, topics, or participant email domain
    2. Workspace description words appear in the text
    3. Partial match of key in participant email domains
    Returns "_readai" when no match is found.
    """
    from ..tools.slack_tools import _get_workspaces

    workspaces = await _get_workspaces()

    title    = (meeting.get("title")   or "").lower()
    summary  = (meeting.get("summary") or "").lower()[:300]

    # Participants JSONB → names + email domains
    parts_raw = meeting.get("participants") or []
    participant_text = ""
    if isinstance(parts_raw, list):
        tokens = []
        for p in parts_raw:
            if isinstance(p, dict):
                tokens.append((p.get("name") or "").lower())
                email = (p.get("email") or "").lower()
                if "@" in email:
                    domain_root = email.split("@")[1].split(".")[0]
                    tokens.extend([email, domain_root])
        participant_text = " ".join(tokens)
    else:
        participant_text = str(parts_raw).lower()

    # Topics JSONB → text
    topics_raw = meeting.get("topics") or []
    topics_text = ""
    if isinstance(topics_raw, list):
        topics_text = " ".join(
            t if isinstance(t, str) else t.get("name", str(t))
            for t in topics_raw
        ).lower()

    full_text = f"{title} {participant_text} {summary} {topics_text}"

    best_key   = ""
    best_score = 0

    for key, info in workspaces.items():
        score = 0
        key_lower = key.lower()
        desc      = (info.get("description") or "").lower()

        if key_lower in full_text:
            score += 3
        if desc:
            matches = sum(1 for w in desc.split() if len(w) > 3 and w in full_text)
            score += matches
        # Email-domain heuristic
        sanitized_key = key_lower.replace("-", "").replace("_", "")
        if sanitized_key in participant_text.replace(".", "").replace("-", "").replace("_", ""):
            score += 2

        if score > best_score:
            best_score = score
            best_key   = key

    return best_key if best_score > 0 else "_readai"


def _parse_action_items(raw) -> list[dict]:
    """Normalise action_items (JSONB list or text) to list of {text, assignee, due_date}."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = []
        import re as _re
        for line in raw.splitlines():
            line = line.strip().lstrip("-• ").strip()
            if len(line) < 5:
                continue
            assignee = ""
            m = _re.search(r"\(([^)]{2,50})\)\s*$", line)
            if m:
                assignee = m.group(1)
                line = line[:m.start()].strip()
            items.append({"text": line, "assignee": assignee, "due_date": ""})
        return items
    if isinstance(raw, list):
        result = []
        for a in raw:
            if isinstance(a, dict):
                text = (a.get("text") or a.get("content") or "").strip()
                if len(text) >= 5:
                    result.append({
                        "text":      text,
                        "assignee":  (a.get("assignee") or a.get("owner") or "").strip(),
                        "due_date":  (a.get("due_date") or "").strip(),
                    })
            elif isinstance(a, str) and len(a.strip()) >= 5:
                result.append({"text": a.strip().lstrip("-• "), "assignee": "", "due_date": ""})
        return result
    return []


async def extract_from_supabase_meetings() -> dict:
    """
    Process Read.ai meetings stored in Supabase that haven't been analysed yet
    (commitments_extracted_at IS NULL).

    For each meeting:
      1. Identify the client by matching workspace keys / descriptions / email domains
      2. Extract action items from the JSONB field (richer than text parsing)
      3. Create pending commitments with proper assignee and due_date
      4. Save a compact meeting summary to the client's memory (topic="reuniões")
      5. Stamp commitments_extracted_at so the meeting isn't processed again

    Returns a summary dict.
    """
    from ..models.database import add_commitment, commitment_source_exists, save_memory
    from datetime import datetime, timezone

    # Only runs when Supabase is configured
    if not os.getenv("SUPABASE_URL"):
        return {"skipped": True, "reason": "SUPABASE_URL not configured"}

    try:
        from ..models.supabase_db import _sb
        sb = await _sb()
    except Exception as exc:
        logger.error("extract_from_supabase_meetings: cannot connect to Supabase: %s", exc)
        return {"skipped": True, "reason": str(exc)}

    # Fetch unprocessed meetings (oldest first so we process in chronological order)
    result = await sb.table("meetings").select(
        "meeting_id,title,meeting_date,participants,summary,"
        "action_items,key_questions,topics,recording_url"
    ).is_("commitments_extracted_at", "null").order(
        "meeting_date", desc=False
    ).limit(50).execute()

    meetings = result.data or []
    if not meetings:
        return {"meetings_processed": 0, "new_commitments": 0, "memories_saved": 0}

    new_commitments = 0
    memories_saved  = 0
    now_iso         = datetime.now(tz=timezone.utc).isoformat()

    for meeting in meetings:
        meeting_id = meeting.get("meeting_id") or ""
        if not meeting_id:
            continue

        title    = meeting.get("title") or "Reunião"
        date_raw = (meeting.get("meeting_date") or "")[:10]
        source_prefix = f"readai:{meeting_id}"

        # 1. Identify client
        client_key = await _identify_client_from_supabase_meeting(meeting)

        # 2. Extract and create commitments
        items = _parse_action_items(meeting.get("action_items"))
        for idx, item in enumerate(items):
            text = item["text"][:500]
            # Use index as source_ts — stable, unique within meeting
            source_ts = str(idx)
            if await commitment_source_exists(source_prefix, source_ts):
                continue

            msg_created_at = f"{date_raw} 00:00:00" if date_raw else None
            await add_commitment(
                client_key=client_key,
                description=text,
                requested_by=f"Read.ai — {title}",
                assigned_to=item["assignee"],
                priority="normal",
                due_date=item["due_date"],
                source_channel=source_prefix,
                source_ts=source_ts,
                msg_created_at=msg_created_at,
            )
            new_commitments += 1

        # 3. Auto-save compact meeting summary to memory
        summary = (meeting.get("summary") or "").strip()
        if client_key != "_readai" and (summary or meeting.get("topics")):
            # Participants
            parts_raw = meeting.get("participants") or []
            if isinstance(parts_raw, list):
                names = [p.get("name") or p.get("email") or "" for p in parts_raw if isinstance(p, dict)]
                participants_str = ", ".join(n for n in names if n)
            else:
                participants_str = str(parts_raw)

            # Topics
            topics_raw = meeting.get("topics") or []
            if isinstance(topics_raw, list):
                topics_str = ", ".join(
                    t if isinstance(t, str) else t.get("name", str(t))
                    for t in topics_raw[:6]
                )
            else:
                topics_str = str(topics_raw)

            # Key questions
            kq_raw = meeting.get("key_questions") or []
            if isinstance(kq_raw, list):
                kq_str = " | ".join(
                    q if isinstance(q, str) else q.get("text", str(q))
                    for q in kq_raw[:3]
                )
            else:
                kq_str = str(kq_raw)

            memory_parts = [f"{date_raw}: {title}"]
            if participants_str:
                memory_parts.append(f"Participantes: {participants_str}")
            if topics_str:
                memory_parts.append(f"Tópicos: {topics_str}")
            if summary:
                memory_parts.append(f"Resumo: {summary[:400]}")
            if kq_str:
                memory_parts.append(f"Questões: {kq_str}")
            if item_count := len(items):
                memory_parts.append(f"{item_count} action item(s) registrado(s)")
            if meeting.get("recording_url"):
                memory_parts.append(f"Gravação: {meeting['recording_url']}")

            await save_memory(
                client_key=client_key,
                topic="reuniões",
                content=" | ".join(memory_parts),
                source="readai_auto",
            )
            memories_saved += 1
            logger.info(
                "Auto-memory saved for %s: %s (%d action items)",
                client_key, title, len(items),
            )

        # 4. Stamp processed timestamp
        try:
            await sb.table("meetings").update(
                {"commitments_extracted_at": now_iso}
            ).eq("meeting_id", meeting_id).execute()
        except Exception as exc:
            logger.warning("Could not stamp commitments_extracted_at for %s: %s", meeting_id, exc)

    summary_out = {
        "meetings_processed": len(meetings),
        "new_commitments":    new_commitments,
        "memories_saved":     memories_saved,
    }
    logger.info("Supabase meeting extraction: %s", summary_out)
    return summary_out
