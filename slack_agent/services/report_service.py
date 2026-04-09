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

async def _fetch_internal_slack(client_key: str, hours_back: int) -> str:
    try:
        from ..tools.slack_tools import _read_tropical_channel
        return await _read_tropical_channel(client_key, hours_back=hours_back, limit=40)
    except Exception as exc:
        return f"(erro ao ler canal interno: {exc})"


async def _fetch_external_slack(client_key: str, hours_back: int) -> str:
    try:
        from ..tools.slack_tools import _get_client_overview
        return await _get_client_overview(client_key, hours_back=hours_back, limit_per_channel=15, exclude_bots=True)
    except Exception as exc:
        return f"(erro ao ler workspace externo: {exc})"


async def _fetch_readai(client_key: str, hours_back: int) -> str:
    try:
        from ..models.database import search_meetings, get_recent_meetings
        rows = await search_meetings(client_key, limit=5)
        if not rows:
            return "Nenhuma reunião encontrada no Read.ai para esse cliente."
        parts = []
        cutoff = time.time() - hours_back * 3600
        for r in rows:
            parts.append(
                f"• {r.get('date','?')[:10]} — {r.get('title','?')}\n"
                f"  Participantes: {r.get('participants','?')}\n"
                f"  Resumo: {(r.get('summary') or '')[:400]}\n"
                f"  Action items: {(r.get('action_items') or '')[:300]}"
            )
        return "\n".join(parts)
    except Exception as exc:
        return f"(erro Read.ai: {exc})"


async def _fetch_hubspot(client_key: str) -> str:
    try:
        from ..tools.hubspot_tools import _hs_client
        hs = _hs_client()
        # Search company by name
        raw = hs.search_companies_by_name(client_key, limit=1)
        import json
        data = json.loads(raw)
        results = data.get("results", [])
        if not results:
            return f"Nenhuma company encontrada no HubSpot para '{client_key}'."
        company = results[0]
        company_id = company.get("id")
        company_name = company.get("properties", {}).get("name", client_key)
        # Get timeline
        timeline_raw = hs.get_company_timeline(company_id, limit=15)
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
        return f"(erro HubSpot: {exc})"


async def _fetch_productive(client_key: str) -> str:
    try:
        from ..tools.productive_tools import _get, _list_projects

        # Step 1: find company by name (fuzzy match)
        data = await _get("/companies", {"page[size]": 200, "filter[archived]": "false"})
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
        projects = await _list_projects(status="active", company_id=company_id, limit=10)
        return f"Empresa Productive: *{company_name}*\n{projects[:600]}"
    except KeyError as exc:
        return f"(Productive não configurado: variável de ambiente {exc} ausente)"
    except Exception as exc:
        return f"(erro Productive: {exc})"


async def _fetch_unanswered(client_key: str) -> str:
    try:
        from ..tools.slack_tools import _check_unanswered
        return await _check_unanswered(client=client_key, threshold_minutes=60)
    except Exception as exc:
        return f"(erro unanswered check: {exc})"


# ---------------------------------------------------------------------------
# Per-client parallel fetch
# ---------------------------------------------------------------------------

async def fetch_client_data(client_key: str, hours_back: int) -> dict:
    """Fetch all data sources for one client concurrently."""
    internal, external, readai, hubspot, productive, unanswered = await asyncio.gather(
        _fetch_internal_slack(client_key, hours_back),
        _fetch_external_slack(client_key, hours_back),
        _fetch_readai(client_key, hours_back),
        _fetch_hubspot(client_key),
        _fetch_productive(client_key),
        _fetch_unanswered(client_key),
        return_exceptions=True,
    )
    def _safe(v):
        return str(v) if isinstance(v, Exception) else v

    return {
        "key": client_key,
        "internal_slack": _safe(internal),
        "external_slack": _safe(external),
        "readai": _safe(readai),
        "hubspot": _safe(hubspot),
        "productive": _safe(productive),
        "unanswered": _safe(unanswered),
    }


# ---------------------------------------------------------------------------
# Report synthesis (single Claude call)
# ---------------------------------------------------------------------------

_REPORT_SYNTHESIS_PROMPT = """Você é o assistente da equipe Tropical. Abaixo estão os dados brutos coletados para cada cliente.
Gere o relatório completo seguindo EXATAMENTE o formato especificado.

FORMATO POR CLIENTE:
━━ 🏢 [NOME DO CLIENTE EM MAIÚSCULAS] ━━━━━━━━━━━━━━━━━━━

📣 *Slack — Canal interno*
• [resumo das mensagens relevantes, ou "Sem atividade no período"]

💬 *Slack — Workspace do cliente*
• [resumo das mensagens do workspace externo, ou "Sem atividade no período"]

📞 *Reuniões (Read.ai)*
• [reuniões encontradas com data, participantes e pontos principais, ou "Nenhuma reunião no período"]

📋 *Timeline HubSpot*
• [engajamentos relevantes: emails, calls, notas, ou "Sem engajamentos no período"]

📊 *Productive*
• [overview rápido de projetos/budget, ou "Sem dados"]

✅ *Acionáveis*
• [compromissos firmes mencionados, com responsável quando possível]

⏳ *Pendências*
• [itens abertos/aguardando resolução]

⚠️ *Alertas* (omita se não houver)
• [mensagens sem resposta, itens críticos]

💡 *Sugestão*
• [1 sugestão objetiva baseada nos dados]

---

Regras:
- Nunca invente informações — use apenas o que está nos dados fornecidos
- Seja conciso mas completo
- Se uma seção não tem dados, escreva "Sem atividade no período" — nunca omita a seção
- Separe cada cliente claramente
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

    # Fetch all clients in parallel
    client_data_list = await asyncio.gather(
        *[fetch_client_data(key, hours_back) for key in client_keys],
        return_exceptions=True,
    )

    # Build context for Claude
    import datetime
    now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    context_parts = [
        f"Período: últimas {hours_back}h | Gerado em: {now_str}\n",
        f"Clientes analisados: {', '.join(client_keys)}\n\n",
    ]

    for data in client_data_list:
        if isinstance(data, Exception):
            continue
        key = data["key"]
        context_parts.append(f"""
=== DADOS BRUTOS: {key.upper()} ===

[SLACK INTERNO - #{key}]
{data['internal_slack'][:1500]}

[SLACK EXTERNO - workspace {key}]
{data['external_slack'][:1500]}

[READ.AI]
{data['readai'][:1000]}

[HUBSPOT TIMELINE]
{data['hubspot'][:1000]}

[PRODUCTIVE]
{data['productive'][:400]}

[MENSAGENS SEM RESPOSTA]
{data['unanswered'][:300]}

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
    r"\b(relatório|relatorio|report|resumo|panorama|status)\b",
    __import__("re").IGNORECASE,
)
_HOURS_RE = __import__("re").compile(r"(\d+)\s*h(?:oras?)?", __import__("re").IGNORECASE)
_DAYS_RE = __import__("re").compile(r"(\d+)\s*dias?", __import__("re").IGNORECASE)


def is_report_request(text: str) -> bool:
    return bool(_REPORT_RE.search(text))


def extract_hours_back(text: str, default: int = 24) -> int:
    m = _HOURS_RE.search(text)
    if m:
        return int(m.group(1))
    m = _DAYS_RE.search(text)
    if m:
        return int(m.group(1)) * 24
    if "hoje" in text.lower():
        return 24
    if "semana" in text.lower():
        return 168
    return default


def extract_client(text: str, available_clients: list[str]) -> Optional[str]:
    """Return a specific client key if mentioned in text, else None (= all clients)."""
    text_lower = text.lower()
    for key in available_clients:
        if key.lower() in text_lower:
            return key
    return None
