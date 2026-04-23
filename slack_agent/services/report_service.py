"""
Fast report generation service.
Pre-fetches all client data in parallel, then calls Claude once for synthesis.
This bypasses the agent loop (which is sequential) and is ~5-10x faster.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Data fetchers (run in parallel per client)
# ---------------------------------------------------------------------------

async def _fetch_all_internal_slack(hours_back: int) -> str:
    """
    Scan ALL accessible Tropical Hub channels in parallel.
    Returns a single block with every channel that had activity, labelled by
    channel name. Claude will decide which channel belongs to which client.
    """
    import os as _os
    token = _os.getenv("TROPICAL_BOT_TOKEN")
    if not token:
        return "⚠️ERRO: TROPICAL_BOT_TOKEN não configurado."
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        from ..tools.slack_tools import _get_tropical_channels, _fmt_ts

        import re as _re

        sc = AsyncWebClient(token=token)
        channels = await _get_tropical_channels(sc)
        logger.info("Internal Slack scan: %d channels, last %dh", len(channels), hours_back)

        oldest = str(time.time() - hours_back * 3600)
        sem = asyncio.Semaphore(15)  # cap concurrent API calls

        # User ID → display name cache (shared across all channels)
        _user_cache: dict[str, str] = {}

        async def _resolve_user(uid: str) -> str:
            if uid not in _user_cache:
                try:
                    info = await sc.users_info(user=uid)
                    u = info.get("user", {})
                    _user_cache[uid] = (
                        u.get("profile", {}).get("display_name")
                        or u.get("real_name")
                        or u.get("name")
                        or uid
                    )
                except Exception:
                    _user_cache[uid] = uid
            return _user_cache[uid]

        async def _resolve_mentions(text: str) -> str:
            """Replace <@UXXX> with @Name in message text."""
            uids = _re.findall(r"<@([A-Z0-9]+)>", text)
            for uid in set(uids):
                name = await _resolve_user(uid)
                text = text.replace(f"<@{uid}>", f"@{name}")
            return text

        # Scale message limit with the time window so longer queries don't miss activity.
        # ~8 messages per day per channel is a reasonable baseline.
        per_channel_limit = max(20, min(200, hours_back // 3))

        async def _read(ch: dict) -> str | None:
            async with sem:
                try:
                    hist = await sc.conversations_history(
                        channel=ch["id"], limit=per_channel_limit, oldest=oldest
                    )
                    msgs = [
                        m for m in hist.get("messages", [])
                        if not m.get("subtype") and not m.get("bot_id")
                    ]
                    if not msgs:
                        return None
                    lines = []
                    for m in reversed(msgs):
                        raw = m.get("text", "").strip()
                        if not raw:
                            continue
                        text = (await _resolve_mentions(raw))[:400]
                        sender = ""
                        if m.get("user"):
                            sender = await _resolve_user(m["user"]) + ": "
                        lines.append(f"  [{_fmt_ts(m.get('ts',''))}] {sender}{text}")
                    return f"#{ch['name']}:\n" + "\n".join(lines) if lines else None
                except Exception:
                    return None

        results = await asyncio.gather(*[_read(ch) for ch in channels])
        sections = [r for r in results if r]

        if not sections:
            return "Sem atividade nos canais internos no período."

        logger.info("Internal Slack: %d/%d channels with activity", len(sections), len(channels))
        return "\n\n".join(sections)
    except Exception as exc:
        logger.error("_fetch_all_internal_slack: %s", exc)
        return f"⚠️ERRO Slack interno: {exc}"


async def _fetch_external_slack(client_key: str, hours_back: int) -> str:
    try:
        from ..tools.slack_tools import _get_client_overview
        # Scale per-channel limit with the time window so longer queries don't miss activity.
        limit = max(20, min(150, hours_back // 2))
        result = await _get_client_overview(client_key, hours_back=hours_back, limit_per_channel=limit, exclude_bots=True)
        if "não encontrado" in result:
            return f"⚠️ERRO: workspace '{client_key}' não configurado no WORKSPACES_JSON."
        return result
    except Exception as exc:
        logger.error("_fetch_external_slack(%s): %s", client_key, exc)
        return f"⚠️ERRO ao ler workspace externo: {exc}"


async def _fetch_readai(client_key: str, hours_back: int) -> str:
    try:
        import datetime as _dt
        import re as _re
        from ..models.database import search_meetings, get_meetings_in_window

        # Compute ISO cutoff for SQL-level filtering
        since_dt = _dt.datetime.utcnow() - _dt.timedelta(hours=hours_back)
        since_iso = since_dt.strftime("%Y-%m-%d")

        # Scale limit with the requested window
        limit = max(20, min(100, hours_back // 2))

        key_lower = client_key.lower()
        # Word-boundary pattern so "ativa" doesn't match "ativação" / "está ativa"
        # and generic Portuguese words inside summaries don't bleed into other clients.
        key_pattern = _re.compile(r'\b' + _re.escape(key_lower) + r'\b')

        def _matches(row: dict) -> bool:
            """True if client_key appears as a whole word in title, participants, or summary."""
            return bool(
                key_pattern.search((row.get("title") or "").lower())
                or key_pattern.search((row.get("participants") or "").lower())
                or key_pattern.search((row.get("summary") or "").lower()[:400])
            )

        # Primary: DB keyword search (broad LIKE) then refine with word-boundary check
        rows = await search_meetings(client_key, limit=limit, since_iso=since_iso)
        rows = [r for r in rows if _matches(r)]

        # Fallback: scan all meetings in window and apply the same word-boundary filter
        if not rows:
            all_rows = await get_meetings_in_window(since_iso, limit=limit)
            rows = [r for r in all_rows if _matches(r)]
            if not rows:
                return "Nenhuma reunião encontrada no Read.ai para esse cliente."

        parts = []
        for r in rows:
            date_str = (r.get("date") or r.get("created_at") or "?")[:10]
            lines = [f"• {date_str} — {r.get('title', '?')}"]
            if r.get("participants"):
                lines.append(f"  Participantes: {r['participants']}")

            # Topics — from Supabase JSONB or plain string
            topics_raw = r.get("topics")
            if topics_raw:
                if isinstance(topics_raw, list):
                    topics_str = ", ".join(
                        t if isinstance(t, str) else str(t.get("name") or t.get("label") or t)
                        for t in topics_raw[:6]
                    )
                else:
                    topics_str = str(topics_raw)
                if topics_str:
                    lines.append(f"  Tópicos: {topics_str}")

            if r.get("summary"):
                lines.append(f"  Resumo: {r['summary'][:400]}")

            # Key questions
            kq_raw = r.get("key_questions")
            if kq_raw:
                if isinstance(kq_raw, list):
                    kq_str = " | ".join(
                        q if isinstance(q, str) else str(q.get("text") or q.get("content") or q)
                        for q in kq_raw[:3]
                    )
                else:
                    kq_str = str(kq_raw)
                if kq_str:
                    lines.append(f"  Questões: {kq_str}")

            if r.get("action_items"):
                lines.append(f"  Action items: {r['action_items'][:300]}")

            if r.get("recording_url"):
                lines.append(f"  Gravação: {r['recording_url']}")

            parts.append("\n".join(lines))
        return "\n".join(parts)
    except Exception as exc:
        logger.error("_fetch_readai(%s): %s", client_key, exc)
        return f"⚠️ERRO Read.ai: {exc}"


async def _fetch_hubspot(client_key: str) -> str:
    try:
        import asyncio
        import json
        from ..tools.hubspot_tools import _hs_client
        hs = _hs_client()
        # Search company by name — run in thread to avoid blocking the event loop
        raw = await asyncio.to_thread(hs.search_companies_by_name, client_key, 1)
        data = json.loads(raw)
        results = data.get("results", [])
        if not results:
            return f"Nenhuma company encontrada no HubSpot para '{client_key}'."
        company = results[0]
        company_id = company.get("id")
        company_name = company.get("properties", {}).get("name", client_key)
        # Get timeline — also in thread
        timeline_raw = await asyncio.to_thread(hs.get_company_timeline, company_id, 15)
        timeline = json.loads(timeline_raw)
        engagements = timeline.get("engagements", [])
        if not engagements:
            return f"Company *{company_name}* encontrada (ID {company_id}) — sem engajamentos recentes."
        lines = [f"Company: *{company_name}* (ID {company_id})"]
        for e in engagements[:10]:
            ts = e.get("created_at", 0)
            date_str = ""
            if ts:
                import datetime
                date_str = datetime.datetime.fromtimestamp(ts / 1000).strftime("%d/%m %H:%M")
            etype = e.get("type", "?")
            subject = e.get("subject") or e.get("body", "")[:100]
            lines.append(f"• [{date_str}] {etype}: {subject}")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("_fetch_hubspot(%s): %s", client_key, exc)
        return f"⚠️ERRO HubSpot: {exc}"


async def _fetch_productive(client_key: str) -> str:
    try:
        import datetime
        from ..tools.productive_tools import _get

        # Step 1: find company by name (fuzzy match)
        data = await _get("/companies", {"page[size]": 200})
        companies = data.get("data", [])
        company_id = None
        company_name = client_key
        for c in companies:
            name = c.get("attributes", {}).get("name", "")
            if client_key.lower() in name.lower():
                company_id = c["id"]
                company_name = name
                break

        if not company_id:
            return f"Nenhuma empresa encontrada no Productive para '{client_key}'."

        # Step 2: get active projects for that company
        proj_data = await _get("/projects", {
            "filter[company_id]": company_id,
            "filter[archived]": "false",
            "page[size]": 50,
        })
        projects = proj_data.get("data", [])
        if not projects:
            return f"Productive: *{company_name}* — sem projetos ativos."

        project_ids = [p["id"] for p in projects]
        project_names = [p.get("attributes", {}).get("name", "?") for p in projects[:3]]

        # Step 3: count overdue open tasks across all projects
        today = datetime.date.today().isoformat()
        overdue_total = 0
        try:
            td = await _get("/tasks", {
                "filter[project_id]": ",".join(project_ids),
                "filter[closed]": "false",
                "filter[due_date_before]": today,
                "page[size]": 1,
            })
            overdue_total = td.get("meta", {}).get("total_count", 0) or 0
        except Exception:
            # Fallback: query per project (first 5 only to avoid rate limits)
            for pid in project_ids[:5]:
                try:
                    td = await _get("/tasks", {
                        "filter[project_id]": pid,
                        "filter[closed]": "false",
                        "filter[due_date_before]": today,
                        "page[size]": 1,
                    })
                    overdue_total += td.get("meta", {}).get("total_count", 0) or 0
                except Exception:
                    pass

        proj_str = ", ".join(project_names)
        if len(projects) > 3:
            proj_str += f" +{len(projects) - 3} mais"
        overdue_str = f"\n• {overdue_total} tarefas overdue" if overdue_total > 0 else "\n• Sem overdue"
        return f"Productive: *{company_name}*\n• Projetos: {proj_str}{overdue_str}"
    except KeyError as exc:
        return f"⚠️ERRO Productive: variável de ambiente {exc} ausente"
    except Exception as exc:
        logger.error("_fetch_productive(%s): %s", client_key, exc)
        return f"⚠️ERRO Productive: {exc}"


async def _fetch_unanswered(client_key: str) -> str:
    try:
        from ..tools.slack_tools import _check_unanswered
        return await _check_unanswered(client=client_key, threshold_minutes=60)
    except Exception as exc:
        return f"(erro unanswered check: {exc})"


async def _fetch_memories(client_key: str) -> str:
    try:
        from ..models.database import recall_memories
        rows = await recall_memories(client_key=client_key, limit=20)
        if not rows:
            return "Nenhuma memória salva para este cliente."

        # Separate preferences from other memories so Claude can use them for tone/personalization
        prefs = [r for r in rows if r.get("topic") == "preferências"]
        rest  = [r for r in rows if r.get("topic") != "preferências"]

        parts = []
        if prefs:
            parts.append("PREFERÊNCIAS DO CLIENTE (use para personalizar tom e abordagem):")
            for r in prefs:
                parts.append(f"  • {r['content']}")
        if rest:
            if prefs:
                parts.append("HISTÓRICO:")
            for r in rest:
                date = r.get("created_at", "")[:10]
                parts.append(f"• [{date} | {r['topic']}] {r['content']}")
        return "\n".join(parts)
    except Exception as exc:
        return f"(erro memória: {exc})"


async def _fetch_commitments(client_key: str) -> str:
    try:
        from ..models.database import list_commitments
        from ..tools.slack_tools import resolve_commitment_users
        pending = await list_commitments(client_key=client_key, status="pending", limit=20)
        recent_done = await list_commitments(client_key=client_key, status="done", limit=5)

        # Resolve user IDs using the correct workspace token per commitment
        pending    = [await resolve_commitment_users(r) for r in pending]
        recent_done = [await resolve_commitment_users(r) for r in recent_done]

        parts = []
        if pending:
            from datetime import datetime, timezone
            lines = []
            for c in pending:
                age = ""
                try:
                    created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    days = (datetime.now(tz=timezone.utc) - created).days
                    age = f" ({days}d pendente)"
                except Exception:
                    pass
                assigned = f" → {c['assigned_to']}" if c.get("assigned_to") else ""
                due = f" | prazo: {c['due_date']}" if c.get("due_date") else ""
                prio = f"[{c.get('priority','normal')}] " if c.get("priority") != "normal" else ""
                lines.append(f"• #{c['id']} {prio}{c['description']}{assigned}{due}{age}")
            parts.append("PENDENTES:\n" + "\n".join(lines))

        if recent_done:
            lines = []
            for c in recent_done:
                date = (c.get("fulfilled_at") or c.get("updated_at") or "")[:10]
                lines.append(f"• #{c['id']} ✅ {c['description']} ({date})")
            parts.append("ENTREGUES RECENTEMENTE:\n" + "\n".join(lines))

        return "\n\n".join(parts) if parts else "Nenhum compromisso registrado."
    except Exception as exc:
        return f"(erro commitments: {exc})"


# ---------------------------------------------------------------------------
# Per-client parallel fetch
# ---------------------------------------------------------------------------

async def fetch_client_data(client_key: str, hours_back: int) -> dict:
    """Fetch per-client data sources concurrently (internal Slack is fetched globally)."""
    external, readai, hubspot, memories, commitments = await asyncio.gather(
        _fetch_external_slack(client_key, hours_back),
        _fetch_readai(client_key, hours_back),
        _fetch_hubspot(client_key),
        _fetch_memories(client_key),
        _fetch_commitments(client_key),
        return_exceptions=True,
    )
    def _safe(v):
        return str(v) if isinstance(v, Exception) else v

    return {
        "key": client_key,
        "external_slack": _safe(external),
        "readai": _safe(readai),
        "hubspot": _safe(hubspot),
        "memories": _safe(memories),
        "commitments": _safe(commitments),
    }


# ---------------------------------------------------------------------------
# Report synthesis (single Claude call)
# ---------------------------------------------------------------------------

_REPORT_SYNTHESIS_PROMPT = """Você é o assistente da equipe Tropical. Gere um briefing focado em conversas, combinados e próximos passos.

FORMATAÇÃO — REGRAS OBRIGATÓRIAS:
• Use APENAS formatação nativa do Slack: *negrito*, _itálico_, `código`, • para listas
• PROIBIDO: tabelas Markdown (|col|), cabeçalhos ## ou ###, linhas ---
• Separe clientes com uma linha vazia simples — sem traços, sem asteriscos repetidos
• Máximo 2 linhas de conteúdo por cliente (⏳/✅ são linhas extras curtas, permitidas)
• Output compacto — sem espaçamento duplo entre itens

FORMATO DE SAÍDA:

📊 *Briefing [Diário/Semanal] — DD/MM/AAAA* — [N] clientes com atividade

🔴 *ClienteX* — [o que foi discutido + quem precisa agir + o quê]
⏳ _"pedido pendente"_ → @Responsável (Nd)

🟠 *ClienteY* — [decisão pendente ou combinado a confirmar]

🟡 *ClienteZ* — [reunião feita / próximo passo definido]
✅ _"o que foi entregue"_

⚪ *ClienteW* — Sem atividade no período.

✅ *Para fazer:*
• [ação concreta] — @Dono

FONTES E O QUE EXTRAIR:
• *Slack interno (Tropical Hub):* discussões e combinados internos da equipe
• *Slack externo (workspace do cliente):* pedidos, problemas e feedbacks do cliente
• *Read.ai:* decisões e action items de reuniões
• *HubSpot:* emails, calls, notas de relacionamento
• *Compromissos rastreados:* mostre ⏳ pendentes (com dias em aberto) e ✅ entregues recentes; pendente critical/high há +3 dias → eleva o cliente para 🔴
• *PREFERÊNCIAS DO CLIENTE* (na seção de memória): use para ajustar tom, detalhe e foco do briefing daquele cliente. Ex: "prefere comunicação direta" → sem rodeios; "contato principal é Maria" → cite o nome
• Fonte com "⚠️ERRO" → escreva _(fonte indisponível)_ — nunca trate como "sem atividade"

CRITÉRIOS DE PRIORIDADE:
🔴 Bloqueio, erro crítico, problema sem resolução visível
🟠 Decisão ou insumo pendente, prazo próximo
🟡 Atividade normal, alinhamento feito, próximo passo claro
⚪ Sem atividade

COMO USAR O SLACK INTERNO TROPICAL HUB:
Dados chegam como dump global de todos os canais. Associe cada canal ao cliente pelo nome do canal, menções ao nome da empresa, ou contexto. Se não conseguir associar com confiança, omita — não invente.

REGRAS FINAIS:
• Ordene do mais crítico (🔴) ao menos (⚪)
• @NomeSobrenome quando o Slack indicar quem deve agir
• Para relatório semanal (hours_back ≥ 72h): adicione ao final um bloco *Para a semana:* com bullets das ações prioritárias — sem tabela
• Nunca invente — só o que está nos dados
"""

async def generate_report(
    client_keys: list[str],
    hours_back: int = 24,
    slack_client=None,
    channel: Optional[str] = None,
) -> str:
    """
    Generate a full report for the given clients.
    Fetches all data in parallel, then synthesizes with a single Claude call.
    """
    t0 = time.time()
    logger.info("Generating report for %d clients, %dh window", len(client_keys), hours_back)

    # Fetch internal Slack ONCE for all clients (parallel channel scan)
    # and per-client data concurrently
    internal_slack_task = asyncio.create_task(_fetch_all_internal_slack(hours_back))
    client_data_list_task = asyncio.gather(
        *[fetch_client_data(key, hours_back) for key in client_keys],
        return_exceptions=True,
    )
    internal_slack_all, client_data_list = await asyncio.gather(
        internal_slack_task, client_data_list_task
    )

    # Build context for Claude — all limits scale with the time window so longer
    # queries don't silently drop activity from earlier in the period.
    import datetime
    now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")

    # Per-field context budgets that grow with hours_back.
    # At 24h the values match the original conservative defaults.
    # At 168h (week) they are ~7x larger so the full week fits.
    _internal_limit  = min(max(6_000,  hours_back * 80),  60_000)
    _external_limit  = min(max(2_000,  hours_back * 40),  20_000)
    _readai_limit    = min(max(1_000,  hours_back * 15),   8_000)
    _hubspot_limit   = min(max(600,    hours_back * 10),   4_000)
    _memories_limit  = min(max(600,    hours_back *  5),   3_000)

    context_parts = [
        f"Período: últimas {hours_back}h | Gerado em: {now_str}\n",
        f"Clientes analisados: {', '.join(client_keys)}\n\n",
        f"=== SLACK INTERNO TROPICAL HUB (TODOS OS CANAIS COM ATIVIDADE) ===\n"
        f"Use estes dados para a seção 💬 Comunicação de cada cliente — associe pelo nome do canal, "
        f"menções de empresa, ou contexto das mensagens.\n\n"
        f"{internal_slack_all[:_internal_limit]}\n\n",
    ]

    for data in client_data_list:
        if isinstance(data, Exception):
            # Don't silently skip — tell Claude this client had a fetch error
            context_parts.append(
                f"\n=== DADOS DO CLIENTE: (ERRO AO CARREGAR) ===\n"
                f"Erro: {data}\n\n"
            )
            continue
        key = data["key"]
        context_parts.append(f"""
=== DADOS DO CLIENTE: {key.upper()} ===

[COMPROMISSOS / PEDIDOS RASTREADOS]
{data['commitments'][:1500]}

[MEMÓRIA PERSISTENTE]
{data['memories'][:_memories_limit]}

[SLACK EXTERNO - workspace {key}]
{data['external_slack'][:_external_limit]}

[READ.AI - reuniões]
{data['readai'][:_readai_limit]}

[HUBSPOT TIMELINE]
{data['hubspot'][:_hubspot_limit]}

""")

    full_context = "".join(context_parts)

    # Single Claude synthesis call
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = await client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=_REPORT_SYNTHESIS_PROMPT,
        messages=[{"role": "user", "content": full_context}],
    )

    report = response.content[0].text
    elapsed = time.time() - t0
    logger.info("Report generated in %.1fs", elapsed)

    header = f"📊 *Relatório Tropical — últimas {hours_back}h* | {now_str}\n{'━'*40}\n\n"
    return header + report


# ---------------------------------------------------------------------------
# Request detection
# ---------------------------------------------------------------------------

_REPORT_RE = __import__("re").compile(
    r"\b(relatórios?|relatorios?|report|resumo|resumos|panorama|status)\b",
    __import__("re").IGNORECASE,
)
_HOURS_RE = __import__("re").compile(r"(\d+)\s*h(?:oras?)?", __import__("re").IGNORECASE)
_DAYS_RE = __import__("re").compile(r"(\d+)\s*dias?", __import__("re").IGNORECASE)


def is_report_request(text: str) -> bool:
    return bool(_REPORT_RE.search(text))


def extract_hours_back(text: str, default: int = 48) -> int:
    m = _HOURS_RE.search(text)
    if m:
        return int(m.group(1))
    m = _DAYS_RE.search(text)
    if m:
        return int(m.group(1)) * 24
    tl = text.lower()
    if "hoje" in tl:
        return 24
    if "ontem" in tl:
        return 48
    # "semana passada" → full previous 7-day week (up to ~200h to be safe)
    if "semana passada" in tl or "última semana" in tl or "ultima semana" in tl:
        return 200
    if "semana" in tl:
        return 168
    if "mês passado" in tl or "mes passado" in tl or "último mês" in tl or "ultimo mes" in tl:
        return 744  # ~31 days
    return default


def extract_client(text: str, available_clients: list[str]) -> Optional[str]:
    """Return a specific client key if mentioned in text, else None (= all clients)."""
    text_lower = text.lower()
    for key in available_clients:
        if key.lower() in text_lower:
            return key
    return None
