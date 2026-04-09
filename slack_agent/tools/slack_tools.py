"""
Slack workspace reader tools for the Claude agent.
Reads messages from external client workspaces using tokens from WORKSPACES_JSON.

WORKSPACES_JSON format (env var):
{
  "galena": {"token": "xoxb-...", "description": "Galena"},
  ...
}
"""
import json
import logging
import os
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace registry
# ---------------------------------------------------------------------------

_workspaces: Optional[dict] = None


def _get_workspaces() -> dict:
    global _workspaces
    if _workspaces is None:
        raw = os.getenv("WORKSPACES_JSON", "{}")
        try:
            _workspaces = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("WORKSPACES_JSON is not valid JSON")
            _workspaces = {}
    return _workspaces


def _client_for(client_key: str) -> Optional[AsyncWebClient]:
    ws = _get_workspaces()
    entry = ws.get(client_key)
    if not entry:
        return None
    return AsyncWebClient(token=entry["token"])


def _client_list() -> list[dict]:
    """Return list of available clients for tool descriptions."""
    ws = _get_workspaces()
    return [{"key": k, "description": v.get("description", k)} for k, v in ws.items()]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SLACK_TOOL_DEFINITIONS = [
    {
        "name": "slack_list_clients",
        "description": "Lista todos os workspaces de clientes disponíveis para leitura.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "slack_list_client_channels",
        "description": (
            "Lista os canais disponíveis no workspace Slack de um cliente específico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'galena', 'sympla'). Use slack_list_clients para ver todos.",
                }
            },
            "required": ["client"],
        },
    },
    {
        "name": "slack_read_client_channel",
        "description": (
            "Lê as mensagens recentes de um canal específico do workspace de um cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "Chave do cliente"},
                "channel_id": {"type": "string", "description": "ID do canal (ex: C01234)"},
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de mensagens (padrão: 30)",
                },
                "hours_back": {
                    "type": "integer",
                    "description": "Buscar mensagens das últimas N horas (padrão: 48)",
                },
            },
            "required": ["client", "channel_id"],
        },
    },
    {
        "name": "slack_get_client_overview",
        "description": (
            "Busca mensagens recentes de todos os canais de um cliente e retorna "
            "um panorama consolidado — ideal para gerar resumos, status e acionáveis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "Chave do cliente"},
                "hours_back": {
                    "type": "integer",
                    "description": "Janela de tempo em horas (padrão: 48)",
                },
                "limit_per_channel": {
                    "type": "integer",
                    "description": "Máx de mensagens por canal (padrão: 20)",
                },
                "exclude_bots": {
                    "type": "boolean",
                    "description": "Ignorar mensagens de bots (padrão: true)",
                },
            },
            "required": ["client"],
        },
    },
]


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

async def execute_slack_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "slack_list_clients":
            clients = _client_list()
            if not clients:
                return "Nenhum workspace de cliente configurado (WORKSPACES_JSON vazio)."
            lines = [f"• *{c['description']}* — chave: `{c['key']}`" for c in clients]
            return "Workspaces disponíveis:\n" + "\n".join(lines)

        elif tool_name == "slack_list_client_channels":
            return await _list_channels(tool_input["client"])

        elif tool_name == "slack_read_client_channel":
            return await _read_channel(
                client_key=tool_input["client"],
                channel_id=tool_input["channel_id"],
                limit=tool_input.get("limit", 30),
                hours_back=tool_input.get("hours_back", 48),
            )

        elif tool_name == "slack_get_client_overview":
            return await _get_client_overview(
                client_key=tool_input["client"],
                hours_back=tool_input.get("hours_back", 48),
                limit_per_channel=tool_input.get("limit_per_channel", 20),
                exclude_bots=tool_input.get("exclude_bots", True),
            )

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("Slack tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


async def _list_channels(client_key: str) -> str:
    sc = _client_for(client_key)
    if not sc:
        return f"Cliente '{client_key}' não encontrado. Use slack_list_clients."

    resp = await sc.conversations_list(
        types="public_channel,private_channel",
        limit=200,
        exclude_archived=True,
    )
    channels = resp.get("channels", [])
    if not channels:
        return f"Nenhum canal encontrado no workspace '{client_key}'."

    lines = [f"• #{c['name']} — ID: `{c['id']}` ({c.get('num_members', '?')} membros)"
             for c in channels]
    return f"Canais de *{client_key}* ({len(channels)}):\n" + "\n".join(lines)


async def _read_channel(
    client_key: str, channel_id: str, limit: int = 30, hours_back: int = 48
) -> str:
    import time

    sc = _client_for(client_key)
    if not sc:
        return f"Cliente '{client_key}' não encontrado."

    oldest = str(time.time() - hours_back * 3600)
    resp = await sc.conversations_history(
        channel=channel_id,
        limit=limit,
        oldest=oldest,
    )
    messages = resp.get("messages", [])
    if not messages:
        return f"Nenhuma mensagem nas últimas {hours_back}h no canal {channel_id}."

    # Resolve user names
    user_cache: dict[str, str] = {}

    async def get_username(uid: str) -> str:
        if uid not in user_cache:
            try:
                info = await sc.users_info(user=uid)
                user_cache[uid] = info["user"].get("real_name") or info["user"].get("name", uid)
            except Exception:
                user_cache[uid] = uid
        return user_cache[uid]

    lines = []
    for msg in reversed(messages):  # oldest first
        if msg.get("subtype"):
            continue
        uid = msg.get("user", "")
        name = await get_username(uid) if uid else "bot"
        ts = msg.get("ts", "")
        text = msg.get("text", "").strip()[:500]  # truncate
        lines.append(f"[{_fmt_ts(ts)}] *{name}*: {text}")

    return "\n".join(lines) if lines else "Sem mensagens de usuários nesse período."


async def _get_client_overview(
    client_key: str,
    hours_back: int = 48,
    limit_per_channel: int = 20,
    exclude_bots: bool = True,
) -> str:
    import time

    sc = _client_for(client_key)
    if not sc:
        return f"Cliente '{client_key}' não encontrado."

    ws_entry = _get_workspaces().get(client_key, {})
    ws_name = ws_entry.get("description", client_key)

    # List channels
    ch_resp = await sc.conversations_list(
        types="public_channel,private_channel",
        limit=100,
        exclude_archived=True,
    )
    channels = ch_resp.get("channels", [])

    oldest = str(time.time() - hours_back * 3600)
    all_sections: list[str] = []

    user_cache: dict[str, str] = {}

    async def get_username(uid: str) -> str:
        if uid not in user_cache:
            try:
                info = await sc.users_info(user=uid)
                user_cache[uid] = info["user"].get("real_name") or info["user"].get("name", uid)
            except Exception:
                user_cache[uid] = uid
        return user_cache[uid]

    for ch in channels:
        try:
            hist = await sc.conversations_history(
                channel=ch["id"],
                limit=limit_per_channel,
                oldest=oldest,
            )
        except Exception:
            continue

        messages = hist.get("messages", [])
        if not messages:
            continue

        lines = []
        for msg in reversed(messages):
            if msg.get("subtype"):
                continue
            if exclude_bots and msg.get("bot_id"):
                continue
            uid = msg.get("user", "")
            name = await get_username(uid) if uid else "bot"
            text = msg.get("text", "").strip()[:300]
            if text:
                lines.append(f"  [{_fmt_ts(msg.get('ts',''))}] {name}: {text}")

        if lines:
            all_sections.append(f"*#{ch['name']}*\n" + "\n".join(lines))

    if not all_sections:
        return f"Nenhuma atividade encontrada em *{ws_name}* nas últimas {hours_back}h."

    header = f"Panorama de *{ws_name}* — últimas {hours_back}h\n{'─'*40}\n"
    return header + "\n\n".join(all_sections)


def _fmt_ts(ts: str) -> str:
    """Convert Slack timestamp to HH:MM."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(float(ts))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ts
