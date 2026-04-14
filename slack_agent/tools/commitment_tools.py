"""
Commitment tracking tools — register, update and query client task/request status.
"""
import logging

from ..models.database import (
    add_commitment, update_commitment,
    list_commitments, get_overdue_commitments,
)

logger = logging.getLogger(__name__)

COMMITMENT_TOOL_DEFINITIONS = [
    {
        "name": "commitment_add",
        "description": (
            "Registra um compromisso ou pedido de cliente: algo que foi solicitado "
            "e precisa ser entregue pela equipe Tropical. Use sempre que o cliente "
            "pedir algo, a equipe se comprometer com uma entrega, ou surgir um "
            "acionável claro com dono definido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'galena', 'canon')",
                },
                "description": {
                    "type": "string",
                    "description": "O que foi pedido ou combinado. Seja específico: 'enviar lista de leads da campanha X' não 'enviar lista'.",
                },
                "requested_by": {
                    "type": "string",
                    "description": "Quem pediu — nome do contato do cliente (ex: 'Pedro Giudice')",
                },
                "assigned_to": {
                    "type": "string",
                    "description": "Quem da Tropical é responsável pela entrega (ex: '@Felipe', '@Gabriela')",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                    "description": "Prioridade: critical=bloqueio/urgente, high=prazo próximo, normal=rotina, low=quando possível",
                },
                "due_date": {
                    "type": "string",
                    "description": "Prazo combinado no formato YYYY-MM-DD (opcional)",
                },
                "source_channel": {
                    "type": "string",
                    "description": "Canal onde o pedido foi feito (ex: '#galena', '#geral')",
                },
            },
            "required": ["client_key", "description"],
        },
    },
    {
        "name": "commitment_list",
        "description": (
            "Lista compromissos/pedidos de um cliente ou de todos os clientes. "
            "Use para ver o que está pendente, o que foi entregue, ou checar "
            "o histórico de um cliente específico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'galena'). Omita para ver todos os clientes.",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "done", "cancelled", "all"],
                    "description": "Filtrar por status. Padrão: 'pending'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de itens retornados (padrão: 30)",
                },
            },
        },
    },
    {
        "name": "commitment_done",
        "description": (
            "Marca um compromisso como entregue (done) ou cancelado. "
            "Use quando a equipe entregar algo que estava pendente, "
            "ou quando o pedido for cancelado/não for mais necessário."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID do compromisso (obtido via commitment_list)",
                },
                "status": {
                    "type": "string",
                    "enum": ["done", "cancelled"],
                    "description": "'done' = entregue, 'cancelled' = cancelado/não necessário",
                },
                "notes": {
                    "type": "string",
                    "description": "Observação sobre a entrega ou motivo do cancelamento (opcional)",
                },
            },
            "required": ["id", "status"],
        },
    },
    {
        "name": "commitment_update",
        "description": (
            "Atualiza campos de um compromisso existente: responsável, prazo ou notas. "
            "Use para refinar informações sem mudar o status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID do compromisso",
                },
                "assigned_to": {
                    "type": "string",
                    "description": "Novo responsável",
                },
                "due_date": {
                    "type": "string",
                    "description": "Novo prazo YYYY-MM-DD",
                },
                "notes": {
                    "type": "string",
                    "description": "Notas adicionais",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "commitment_overdue",
        "description": (
            "Lista todos os compromissos pendentes há mais de N dias. "
            "Use para identificar o que está atrasado e precisa de atenção imediata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_old": {
                    "type": "integer",
                    "description": "Considerar atrasado se pendente há mais de N dias (padrão: 3)",
                },
            },
        },
    },
]


async def execute_commitment_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "commitment_add":
            return await _add(tool_input)
        elif tool_name == "commitment_list":
            return await _list(tool_input)
        elif tool_name == "commitment_done":
            return await _done(tool_input)
        elif tool_name == "commitment_update":
            return await _update(tool_input)
        elif tool_name == "commitment_overdue":
            return await _overdue(tool_input)
        else:
            return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("commitment tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_PRIORITY_EMOJI = {"critical": "🔴", "high": "🟠", "normal": "🟡", "low": "⚪"}


def _fmt(c: dict) -> str:
    age = ""
    if c.get("created_at"):
        try:
            from datetime import datetime, timezone
            created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days = (datetime.now(tz=timezone.utc) - created).days
            age = f" | {days}d atrás"
        except Exception:
            pass
    emoji = _PRIORITY_EMOJI.get(c.get("priority", "normal"), "🟡")
    parts = [f"{emoji} *[#{c['id']}]* {c['description']}"]
    if c.get("requested_by"):
        parts.append(f"   Pedido por: {c['requested_by']}")
    if c.get("assigned_to"):
        parts.append(f"   Responsável: {c['assigned_to']}")
    if c.get("due_date"):
        parts.append(f"   Prazo: {c['due_date']}")
    if c.get("notes"):
        parts.append(f"   Nota: {c['notes']}")
    parts[0] += age
    if c.get("fulfilled_at"):
        parts.append(f"   ✅ Entregue em: {c['fulfilled_at'][:10]}")
    return "\n".join(parts)


async def _add(inp: dict) -> str:
    cid = await add_commitment(
        client_key=inp.get("client_key", ""),
        description=inp.get("description", ""),
        requested_by=inp.get("requested_by", ""),
        assigned_to=inp.get("assigned_to", ""),
        priority=inp.get("priority", "normal"),
        due_date=inp.get("due_date", ""),
        source_channel=inp.get("source_channel", ""),
    )
    client = inp.get("client_key", "?")
    desc = inp.get("description", "?")
    assigned = inp.get("assigned_to", "")
    resp = f"✅ Compromisso #{cid} registrado para *{client}*: _{desc}_"
    if assigned:
        resp += f"\nResponsável: {assigned}"
    return resp


async def _resolve_rows(rows: list[dict]) -> list[dict]:
    """Resolve Slack user IDs using the correct workspace token per commitment."""
    try:
        from .slack_tools import resolve_commitment_users
        rows = [await resolve_commitment_users(r) for r in rows]
    except Exception:
        pass
    return rows


async def _list(inp: dict) -> str:
    client_key = inp.get("client_key") or None
    status_filter = inp.get("status", "pending")
    limit = int(inp.get("limit", 30))

    if status_filter == "all":
        status_filter = None

    rows = await list_commitments(client_key=client_key, status=status_filter, limit=limit)
    rows = await _resolve_rows(rows)
    if not rows:
        scope = f"*{client_key}*" if client_key else "todos os clientes"
        st = f" com status `{inp.get('status','pending')}`" if inp.get("status") else " pendentes"
        return f"Nenhum compromisso{st} para {scope}."

    # Group by client
    by_client: dict[str, list] = {}
    for r in rows:
        by_client.setdefault(r["client_key"], []).append(r)

    lines = []
    for ck, items in by_client.items():
        lines.append(f"\n*{ck.upper()}* ({len(items)} item{'s' if len(items) != 1 else ''}):")
        for c in items:
            lines.append(_fmt(c))
    return "\n".join(lines).strip()


async def _done(inp: dict) -> str:
    cid = int(inp.get("id", 0))
    status = inp.get("status", "done")
    notes = inp.get("notes", "")
    ok = await update_commitment(cid, status=status, notes=notes or None)
    if not ok:
        return f"Compromisso #{cid} não encontrado."
    verb = "entregue ✅" if status == "done" else "cancelado ❌"
    return f"Compromisso #{cid} marcado como {verb}." + (f"\nNota: {notes}" if notes else "")


async def _update(inp: dict) -> str:
    cid = int(inp.get("id", 0))
    ok = await update_commitment(
        cid,
        assigned_to=inp.get("assigned_to"),
        due_date=inp.get("due_date"),
        notes=inp.get("notes"),
    )
    if not ok:
        return f"Compromisso #{cid} não encontrado."
    return f"Compromisso #{cid} atualizado."


async def _overdue(inp: dict) -> str:
    days = int(inp.get("days_old", 3))
    rows = await get_overdue_commitments(days_old=days)
    rows = await _resolve_rows(rows)
    if not rows:
        return f"Nenhum compromisso pendente há mais de {days} dias. 👍"

    by_client: dict[str, list] = {}
    for r in rows:
        by_client.setdefault(r["client_key"], []).append(r)

    lines = [f"⚠️ *{len(rows)} compromisso(s) pendente(s) há +{days} dias:*\n"]
    for ck, items in by_client.items():
        lines.append(f"*{ck.upper()}*:")
        for c in items:
            lines.append(_fmt(c))
    return "\n".join(lines)
