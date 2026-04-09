"""
Slack workspace reader tools for the Claude agent.
Reads messages from external client workspaces.

Workspace sources (merged, DB takes precedence over env):
  1. WORKSPACES_JSON env var (static, set in Fly.io secrets)
  2. `workspaces` SQLite table (dynamic, managed via bot commands)

WORKSPACES_JSON format:
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

from ..models.database import add_workspace, remove_workspace, list_db_workspaces

logger = logging.getLogger(__name__)


def _get_env_workspaces() -> dict:
    """Return workspaces defined in WORKSPACES_JSON env var."""
    raw = os.getenv("WORKSPACES_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("WORKSPACES_JSON is not valid JSON")
        return {}


async def _get_workspaces() -> dict:
    """Return merged workspace registry: env + DB (DB takes precedence)."""
    merged = _get_env_workspaces()
    db_rows = await list_db_workspaces()
    for row in db_rows:
        merged[row["key"]] = {"token": row["token"], "description": row.get("description", row["key"])}
    return merged


async def _client_for(client_key: str) -> Optional[AsyncWebClient]:
    ws = await _get_workspaces()
    entry = ws.get(client_key)
    if not entry:
        return None
    return AsyncWebClient(token=entry["token"])


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
    {
        "name": "slack_manage_workspace",
        "description": (
            "Gerencia workspaces de clientes Slack: adiciona, remove ou lista workspaces "
            "salvos no banco de dados. Use para registrar novos clientes sem precisar "
            "alterar variáveis de ambiente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "'add' para adicionar, 'remove' para remover, 'list' para listar workspaces do banco",
                },
                "key": {
                    "type": "string",
                    "description": "Chave única do workspace, sem espaços (ex: 'novo-cliente'). Obrigatório para add e remove.",
                },
                "token": {
                    "type": "string",
                    "description": "Token Slack xoxb-... do workspace do cliente. Obrigatório para add.",
                },
                "description": {
                    "type": "string",
                    "description": "Nome amigável do cliente (ex: 'Cliente Novo'). Obrigatório para add.",
                },
            },
            "required": ["action"],
        },
    },
]


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

async def execute_slack_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "slack_list_clients":
            ws = await _get_workspaces()
            if not ws:
                return "Nenhum workspace de cliente configurado."
            lines = [f"• *{v.get('description', k)}* — chave: `{k}`" for k, v in ws.items()]
            return f"Workspaces disponíveis ({len(ws)}):\n" + "\n".join(lines)

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

        elif tool_name == "slack_manage_workspace":
            return await _manage_workspace(tool_input)

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("Slack tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


async def _manage_workspace(tool_input: dict) -> str:
    action = tool_input.get("action")

    if action == "list":
        db_rows = await list_db_workspaces()
        if not db_rows:
            return "Nenhum workspace salvo no banco de dados. Os workspaces da variável de ambiente ainda estão disponíveis."
        lines = [
            f"• *{r.get('description', r['key'])}* — chave: `{r['key']}` (adicionado em {r.get('created_at', '?')[:10]})"
            for r in db_rows
        ]
        return f"Workspaces salvos no banco ({len(db_rows)}):\n" + "\n".join(lines)

    elif action == "add":
        key = tool_input.get("key", "").strip().lower().replace(" ", "-")
        token = tool_input.get("token", "").strip()
        description = tool_input.get("description", "").strip()

        if not key:
            return "Erro: `key` é obrigatório para adicionar um workspace."
        if not token or not token.startswith("xoxb-"):
            return "Erro: `token` deve ser um token Slack válido começando com `xoxb-`."
        if not description:
            return "Erro: `description` é obrigatório para adicionar um workspace."

        is_new = await add_workspace(key, token, description)
        action_word = "adicionado" if is_new else "atualizado"
        return (
            f"Workspace *{description}* (chave: `{key}`) {action_word} com sucesso.\n"
            f"Use `slack_list_client_channels` com `client: \"{key}\"` para listar os canais."
        )

    elif action == "remove":
        key = tool_input.get("key", "").strip()
        if not key:
            return "Erro: `key` é obrigatório para remover um workspace."
        removed = await remove_workspace(key)
        if removed:
            return f"Workspace `{key}` removido do banco de dados."
        else:
            return f"Workspace `{key}` não encontrado no banco. Workspaces da variável de ambiente não podem ser removidos por aqui."

    else:
        return f"Ação desconhecida: `{action}`. Use 'add', 'remove' ou 'list'."


async def _list_channels(client_key: str) -> str:
    sc = await _client_for(client_key)
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

    sc = await _client_for(client_key)
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
        text = msg.get("text", "").strip()[:500]
        lines.append(f"[{_fmt_ts(ts)}] *{name}*: {text}")

    return "\n".join(lines) if lines else "Sem mensagens de usuários nesse período."


async def _get_client_overview(
    client_key: str,
    hours_back: int = 48,
    limit_per_channel: int = 20,
    exclude_bots: bool = True,
) -> str:
    import time

    sc = await _client_for(client_key)
    if not sc:
        return f"Cliente '{client_key}' não encontrado."

    ws = await _get_workspaces()
    ws_name = ws.get(client_key, {}).get("description", client_key)

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
    """Convert Slack timestamp to DD/MM HH:MM."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(float(ts))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ts
