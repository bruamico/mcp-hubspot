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
import time as _time
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

from ..models.database import (
    add_workspace, remove_workspace, list_db_workspaces,
    set_channel_mapping, get_channel_mapping, list_channel_mappings,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Channel list cache — avoids 12 redundant API calls per report run
# (one per client). Invalidated after 5 minutes.
# ---------------------------------------------------------------------------
_tropical_channels_cache: tuple[float, list] | None = None  # (fetched_at, channels)
_CHANNEL_CACHE_TTL = 300  # 5 minutes


async def _get_tropical_channels(sc: AsyncWebClient) -> list[dict]:
    """Return all non-archived channels from the Tropical Hub workspace, with cache."""
    global _tropical_channels_cache
    now = _time.time()
    if _tropical_channels_cache and now - _tropical_channels_cache[0] < _CHANNEL_CACHE_TTL:
        return _tropical_channels_cache[1]

    channels = await _paginated_channels(sc)
    _tropical_channels_cache = (now, channels)
    logger.info("Tropical Hub channel list refreshed: %d channels", len(channels))
    return channels


async def _paginated_channels(
    sc: AsyncWebClient,
    types: str = "public_channel,private_channel",
) -> list[dict]:
    """Fetch ALL channels from a workspace using cursor-based pagination."""
    all_channels: list[dict] = []
    cursor: Optional[str] = None
    page = 0
    while True:
        page += 1
        kwargs: dict = {"types": types, "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = await sc.conversations_list(**kwargs)
        except Exception as exc:
            logger.warning("conversations_list page %d failed: %s", page, exc)
            break
        all_channels.extend(resp.get("channels", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return all_channels


def _get_env_workspaces() -> dict:
    """Return workspaces defined in WORKSPACES_JSON env var."""
    raw = os.getenv("WORKSPACES_JSON", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("WORKSPACES_JSON is not valid JSON")
        return {}


# Global user ID → display name cache: uid → name
# Keyed only by uid since Slack user IDs are globally unique.
_user_name_cache: dict[str, str] = {}

_UID_RE = __import__("re").compile(r"<@([A-Z0-9]{6,12})>|@([A-Z0-9]{6,12})\b")


async def _resolve_uid(uid: str, clients: list) -> str:
    """
    Try each AsyncWebClient in *clients* until users_info succeeds.
    Returns display name or raw uid on failure.
    """
    if uid in _user_name_cache:
        return _user_name_cache[uid]
    for sc in clients:
        try:
            info = await sc.users_info(user=uid)
            u = info.get("user", {})
            name = (
                u.get("profile", {}).get("display_name")
                or u.get("real_name")
                or u.get("name")
                or uid
            )
            if name and name != uid:
                _user_name_cache[uid] = name
                return name
        except Exception:
            continue
    _user_name_cache[uid] = uid
    return uid


async def _replace_uids_in_text(text: str, clients: list) -> str:
    if not text:
        return text
    flat_ids = {a or b for a, b in _UID_RE.findall(text) if (a or b)}
    for uid in flat_ids:
        name = await _resolve_uid(uid, clients)
        text = text.replace(f"<@{uid}>", f"@{name}").replace(f"@{uid}", f"@{name}")
    return text


async def resolve_user_ids(text: str) -> str:
    """Replace @UXXXXXXX IDs in text using TROPICAL_BOT_TOKEN."""
    token = os.getenv("TROPICAL_BOT_TOKEN", "")
    if not token or not text:
        return text
    return await _replace_uids_in_text(text, [AsyncWebClient(token=token)])


async def resolve_commitment_users(row: dict) -> dict:
    """
    Resolve Slack user IDs in a commitment row's assigned_to and requested_by.

    Picks the right workspace token from source_channel:
      - "ext:galena#general"  → tries Galena token first, then TROPICAL_BOT_TOKEN
      - "internal#..."        → uses TROPICAL_BOT_TOKEN
      - "readai:..."          → tries TROPICAL_BOT_TOKEN (best effort)
    """
    source_channel = row.get("source_channel", "")
    tropical_token = os.getenv("TROPICAL_BOT_TOKEN", "")

    clients: list[AsyncWebClient] = []

    if source_channel.startswith("ext:"):
        workspace_key = source_channel[4:].split("#")[0]
        ws = await _get_workspaces()
        ws_token = ws.get(workspace_key, {}).get("token", "")
        if ws_token:
            clients.append(AsyncWebClient(token=ws_token))
    if tropical_token:
        clients.append(AsyncWebClient(token=tropical_token))

    if not clients:
        return row

    for field in ("assigned_to", "requested_by", "description"):
        if row.get(field):
            row[field] = await _replace_uids_in_text(row[field], clients)

    return row


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
    {
        "name": "slack_read_tropical_channel",
        "description": (
            "Lê mensagens de um canal interno da Tropical Hub (o workspace do próprio time). "
            "Use para buscar mensagens sobre um cliente específico no canal interno correspondente, "
            "ex: canal #galena para o cliente Galena."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name": {
                    "type": "string",
                    "description": "Nome do canal sem # (ex: 'galena', 'geral')",
                },
                "hours_back": {
                    "type": "integer",
                    "description": "Buscar mensagens das últimas N horas (padrão: 48)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de mensagens (padrão: 50)",
                },
            },
            "required": ["channel_name"],
        },
    },
    {
        "name": "slack_check_unanswered",
        "description": (
            "Verifica se há mensagens de clientes em workspaces externos sem resposta há mais de N minutos. "
            "Útil para detectar clientes aguardando retorno."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {
                    "type": "string",
                    "description": "Chave do workspace do cliente. Se omitido, verifica todos.",
                },
                "threshold_minutes": {
                    "type": "integer",
                    "description": "Tempo em minutos sem resposta para considerar pendente (padrão: 60)",
                },
            },
        },
    },
    {
        "name": "slack_map_channel",
        "description": (
            "Define ou lista o mapeamento entre chave de cliente e canal interno "
            "da Tropical Hub. Use quando o nome do canal não coincide com a chave "
            "do cliente (ex: cliente 'globalthings' → canal '#gt')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "list"],
                    "description": "'set' para definir mapeamento, 'list' para listar todos",
                },
                "client_key": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'globalthings'). Obrigatório para set.",
                },
                "channel_name": {
                    "type": "string",
                    "description": "Nome do canal sem # (ex: 'gt'). Obrigatório para set.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "monitor_client",
        "description": (
            "Cria, lista ou remove jobs de monitoramento de delta. "
            "O monitor roda a cada N minutos, compara o estado atual dos clientes com o snapshot anterior "
            "e posta no Slack apenas quando há mudança urgente (nova mensagem crítica, combinado pendente, etc). "
            "Diferente do relatório agendado: só avisa quando algo muda, não repete o mesmo conteúdo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "'add' para criar, 'remove' para remover, 'list' para listar",
                },
                "client": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'galena'). Omita para monitorar todos.",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo em minutos entre verificações (padrão: 60)",
                },
                "channel": {
                    "type": "string",
                    "description": "Canal Slack para receber alertas (ex: '#geral' ou ID do canal)",
                },
                "min_priority": {
                    "type": "string",
                    "enum": ["red", "orange", "yellow"],
                    "description": (
                        "Prioridade mínima para disparar alerta. "
                        "'orange' = só 🔴🟠 (padrão), 'yellow' = qualquer mudança, 'red' = só crítico"
                    ),
                },
                "job_id": {
                    "type": "integer",
                    "description": "ID do job (obrigatório para remove)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "schedule_report",
        "description": (
            "Cria, lista ou remove relatórios automáticos agendados. "
            "Exemplo: relatório de todos os clientes a cada hora no canal #geral."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "delete"],
                    "description": "'create' para criar, 'list' para listar, 'delete' para remover",
                },
                "client": {
                    "type": "string",
                    "description": "Chave do cliente (ex: 'galena'). Omita para relatório de todos.",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo em minutos entre relatórios (ex: 60 para de hora em hora)",
                },
                "hours_back": {
                    "type": "integer",
                    "description": "Janela de tempo do relatório em horas (padrão: 24)",
                },
                "channel": {
                    "type": "string",
                    "description": "Canal Slack onde enviar (ex: 'C01234' ou '#geral')",
                },
                "report_id": {
                    "type": "integer",
                    "description": "ID do relatório agendado (obrigatório para delete)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "slack_client_map",
        "description": (
            "Gera uma tabela de mapeamento de todos os clientes: chave do workspace, "
            "descrição, canal interno correspondente e empresa encontrada no HubSpot "
            "(com ID e domínio). Use para verificar se os nomes estão corretos e se "
            "cada cliente tem uma company correspondente no CRM."
        ),
        "input_schema": {"type": "object", "properties": {}},
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

        elif tool_name == "slack_read_tropical_channel":
            return await _read_tropical_channel(
                channel_name=tool_input["channel_name"],
                hours_back=tool_input.get("hours_back", 48),
                limit=tool_input.get("limit", 50),
            )

        elif tool_name == "slack_check_unanswered":
            return await _check_unanswered(
                client=tool_input.get("client"),
                threshold_minutes=tool_input.get("threshold_minutes", 60),
            )

        elif tool_name == "slack_map_channel":
            return await _map_channel(tool_input)

        elif tool_name == "monitor_client":
            return await _manage_monitor(tool_input)

        elif tool_name == "schedule_report":
            return await _schedule_report(tool_input)

        elif tool_name == "slack_client_map":
            return await _client_map()

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


async def _map_channel(tool_input: dict) -> str:
    action = tool_input.get("action")
    if action == "list":
        rows = await list_channel_mappings()
        if not rows:
            return "Nenhum mapeamento salvo. Use `slack_map_channel` com `action: set` para configurar."
        lines = [f"• `{r['client_key']}` → `#{r['channel_name']}`" for r in rows]
        return f"Mapeamentos de canais internos ({len(rows)}):\n" + "\n".join(lines)
    elif action == "set":
        key = tool_input.get("client_key", "").strip().lower()
        ch = tool_input.get("channel_name", "").strip().lstrip("#").lower()
        if not key or not ch:
            return "Erro: `client_key` e `channel_name` são obrigatórios."
        await set_channel_mapping(key, ch)
        return f"Mapeamento salvo: cliente `{key}` → canal `#{ch}`.\nPróximo relatório usará este canal."
    return f"Ação desconhecida: {action}"


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

    # Paginate through ALL channels — limit=100 would silently miss active channels
    channels = await _paginated_channels(sc)
    logger.info("_get_client_overview(%s): %d channels found", client_key, len(channels))

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
            raw_text = msg.get("text", "").strip()
            # Resolve <@UXXX> mentions to names
            import re as _re
            for mentioned_uid in set(_re.findall(r"<@([A-Z0-9]+)>", raw_text)):
                mentioned_name = await get_username(mentioned_uid)
                raw_text = raw_text.replace(f"<@{mentioned_uid}>", f"@{mentioned_name}")
            text = raw_text[:300]
            if text:
                lines.append(f"  [{_fmt_ts(msg.get('ts',''))}] {name}: {text}")

        if lines:
            all_sections.append(f"*#{ch['name']}*\n" + "\n".join(lines))

    if not all_sections:
        return f"Nenhuma atividade encontrada em *{ws_name}* nas últimas {hours_back}h."

    header = f"Panorama de *{ws_name}* — últimas {hours_back}h\n{'─'*40}\n"
    return header + "\n\n".join(all_sections)


async def _read_tropical_channel(channel_name: str, hours_back: int = 48, limit: int = 50) -> str:
    """Read messages from an internal Tropical Hub channel using the bot token."""
    import time
    token = os.getenv("TROPICAL_BOT_TOKEN")
    if not token:
        return "TROPICAL_BOT_TOKEN não configurado."

    sc = AsyncWebClient(token=token)
    # Check explicit mapping first (client_key → channel_name)
    mapped = await get_channel_mapping(channel_name)
    search_name = mapped if mapped else channel_name.lstrip("#").lower()

    # Use cached + paginated channel list (avoids redundant API calls across clients)
    channels = await _get_tropical_channels(sc)
    name_lower = search_name.lower()

    # Exact match, then substring match
    matched = [c for c in channels if c["name"].lower() == name_lower]
    if not matched:
        matched = [c for c in channels if name_lower in c["name"].lower()]
    if not matched:
        available = ", ".join(f"#{c['name']}" for c in channels[:50])
        return (
            f"Canal para `{channel_name}` não encontrado ({len(channels)} canais verificados). "
            f"Use `slack_map_channel` para mapear manualmente. Canais disponíveis: {available}"
        )
    # Use the best match (exact preferred; among fuzzy, shortest name wins)
    ch = matched[0] if len(matched) == 1 else min(matched, key=lambda c: len(c["name"]))

    oldest = str(time.time() - hours_back * 3600)
    try:
        hist = await sc.conversations_history(channel=ch["id"], limit=limit, oldest=oldest)
    except Exception as e:
        if "not_in_channel" in str(e):
            # Try to join public channel automatically
            if not ch.get("is_private"):
                try:
                    await sc.conversations_join(channel=ch["id"])
                    hist = await sc.conversations_history(channel=ch["id"], limit=limit, oldest=oldest)
                except Exception as join_err:
                    return f"Canal `#{channel_name}` encontrado mas não acessível: {join_err}"
            else:
                return f"Canal `#{ch['name']}` é privado — adicione o bot manualmente ao canal para habilitar leitura."
        else:
            raise
    messages = hist.get("messages", [])
    if not messages:
        return f"Nenhuma mensagem nas últimas {hours_back}h no canal #{channel_name}."

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
    for msg in reversed(messages):
        if msg.get("subtype") or msg.get("bot_id"):
            continue
        uid = msg.get("user", "")
        name = await get_username(uid) if uid else "bot"
        text = msg.get("text", "").strip()[:400]
        if text:
            lines.append(f"[{_fmt_ts(msg.get('ts', ''))}] *{name}*: {text}")

    return f"Canal interno *#{channel_name}* — últimas {hours_back}h:\n" + "\n".join(lines) if lines else f"Sem mensagens de usuários em #{channel_name} nesse período."


async def _check_unanswered(client: Optional[str] = None, threshold_minutes: int = 60) -> str:
    """Check for unanswered client messages across external workspaces."""
    import time
    ws = await _get_workspaces()
    clients_to_check = {client: ws[client]} if client and client in ws else ws
    threshold_ts = time.time() - threshold_minutes * 60
    alerts = []

    for key, entry in clients_to_check.items():
        sc = AsyncWebClient(token=entry["token"])
        try:
            ch_resp = await sc.conversations_list(
                types="public_channel,private_channel", limit=50, exclude_archived=True
            )
        except Exception:
            continue

        for ch in ch_resp.get("channels", []):
            try:
                hist = await sc.conversations_history(channel=ch["id"], limit=10)
            except Exception:
                continue
            msgs = [m for m in hist.get("messages", []) if not m.get("subtype") and not m.get("bot_id")]
            if not msgs:
                continue
            last = msgs[0]  # most recent
            last_ts = float(last.get("ts", 0))
            if last_ts < threshold_ts:
                alerts.append(
                    f"• *{entry.get('description', key)}* `#{ch['name']}`: "
                    f"última mensagem há {int((time.time() - last_ts) / 60)}min sem resposta"
                )

    if not alerts:
        return f"Nenhum cliente com mensagem sem resposta há mais de {threshold_minutes} minutos."
    return f"⚠️ Mensagens sem resposta (>{threshold_minutes}min):\n" + "\n".join(alerts)


async def _manage_monitor(tool_input: dict) -> str:
    """Create, list or remove delta monitoring jobs."""
    from ..models.database import (
        add_monitoring_job, remove_monitoring_job, list_monitoring_jobs,
    )

    action = tool_input.get("action")

    if action == "list":
        jobs = await list_monitoring_jobs()
        if not jobs:
            return "Nenhum job de monitoramento ativo."
        lines = []
        for j in jobs:
            client_str = j["client"] or "todos os clientes"
            last = (j.get("last_run_at") or "nunca executado")[:16]
            priority_map = {"red": "🔴 só crítico", "orange": "🔴🟠", "yellow": "🔴🟠🟡"}
            prio = priority_map.get(j["min_priority"], j["min_priority"])
            lines.append(
                f"• ID `{j['id']}` — *{client_str}* — a cada {j['interval_minutes']}min — "
                f"canal `{j['channel']}` — alertas: {prio} — último: {last}"
            )
        return f"Monitoramentos ativos ({len(jobs)}):\n" + "\n".join(lines)

    elif action == "add":
        client = tool_input.get("client")
        interval = int(tool_input.get("interval_minutes", 60))
        channel = tool_input.get("channel", os.getenv("SLACK_REPORT_CHANNEL", "#geral"))
        min_priority = tool_input.get("min_priority", "orange")

        if interval < 5:
            return "Erro: intervalo mínimo é 5 minutos."

        job_id = await add_monitoring_job(client, interval, channel, min_priority)
        client_str = f"*{client}*" if client else "*todos os clientes*"
        priority_map = {"red": "só 🔴 crítico", "orange": "🔴🟠 urgente/atenção", "yellow": "🔴🟠🟡 qualquer mudança"}
        prio_str = priority_map.get(min_priority, min_priority)
        return (
            f"✅ Monitoramento criado (ID `{job_id}`):\n"
            f"• Clientes: {client_str}\n"
            f"• Intervalo: a cada {interval} minutos\n"
            f"• Canal: {channel}\n"
            f"• Alertas: {prio_str}\n"
            f"O monitor compara o estado atual com o snapshot anterior e posta apenas quando algo muda."
        )

    elif action == "remove":
        job_id = tool_input.get("job_id")
        if not job_id:
            return "Erro: `job_id` é obrigatório para remover um monitoramento."
        removed = await remove_monitoring_job(int(job_id))
        if removed:
            return f"Monitoramento ID `{job_id}` removido."
        return f"ID `{job_id}` não encontrado."

    return f"Ação desconhecida: {action}"


async def _schedule_report(tool_input: dict) -> str:
    """Create, list or delete scheduled reports."""
    from ..models.database import DB_PATH
    import aiosqlite

    action = tool_input.get("action")

    if action == "list":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, client, interval_minutes, hours_back, channel, active, last_run_at FROM scheduled_reports WHERE active=1 ORDER BY id"
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return "Nenhum relatório agendado ativo."
        lines = []
        for r in rows:
            client_str = r["client"] or "todos os clientes"
            lines.append(
                f"• ID `{r['id']}` — *{client_str}* — a cada {r['interval_minutes']}min — "
                f"janela {r['hours_back']}h — canal `{r['channel']}` — "
                f"último: {(r['last_run_at'] or 'nunca')[:16]}"
            )
        return f"Relatórios agendados ({len(rows)}):\n" + "\n".join(lines)

    elif action == "create":
        interval = tool_input.get("interval_minutes")
        channel = tool_input.get("channel", os.getenv("SLACK_REPORT_CHANNEL", "#geral"))
        hours_back = tool_input.get("hours_back", 24)
        client = tool_input.get("client")
        if not interval:
            return "Erro: `interval_minutes` é obrigatório para criar um relatório agendado."
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO scheduled_reports (client, interval_minutes, hours_back, channel, active) VALUES (?,?,?,?,1)",
                (client, interval, hours_back, channel),
            )
            await db.commit()
            report_id = cur.lastrowid
        client_str = client or "todos os clientes"
        return (
            f"Relatório agendado criado (ID `{report_id}`):\n"
            f"• Cliente: *{client_str}*\n"
            f"• Frequência: a cada {interval} minutos\n"
            f"• Janela: últimas {hours_back}h\n"
            f"• Canal: {channel}\n"
            f"O primeiro relatório será enviado na próxima janela."
        )

    elif action == "delete":
        report_id = tool_input.get("report_id")
        if not report_id:
            return "Erro: `report_id` é obrigatório para remover um relatório agendado."
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE scheduled_reports SET active=0 WHERE id=?", (report_id,)
            )
            await db.commit()
        if cur.rowcount:
            return f"Relatório agendado ID `{report_id}` removido."
        return f"ID `{report_id}` não encontrado."

    return f"Ação desconhecida: {action}"


async def _client_map() -> str:
    """
    Build a verification table: workspace key → internal channel → HubSpot company.
    Searches HubSpot by workspace key first, then by description, picks best match.
    """
    import json as _json
    import asyncio as _asyncio

    ws = await _get_workspaces()
    ch_rows = await list_channel_mappings()
    ch_map = {r["client_key"]: r["channel_name"] for r in ch_rows}

    # Get HubSpot portal ID for building direct URLs
    portal_id = ""
    try:
        import aiohttp as _aiohttp
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN", "")
        async with _aiohttp.ClientSession() as _sess:
            async with _sess.get(
                f"https://api.hubapi.com/oauth/v1/access-tokens/{token}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_aiohttp.ClientTimeout(total=8),
            ) as _resp:
                if _resp.status == 200:
                    portal_id = (await _resp.json()).get("hub_id", "")
    except Exception:
        portal_id = ""

    def _hub_url(company_id: str) -> str:
        if portal_id:
            return f"https://app.hubspot.com/contacts/{portal_id}/company/{company_id}"
        return f"https://app.hubspot.com/contacts/companies/{company_id}"

    try:
        from ..tools.hubspot_tools import _hs_client
        hs = _hs_client()
    except Exception as exc:
        return f"⚠️ HubSpot não disponível: {exc}"

    rows: list[str] = []

    for key, entry in ws.items():
        desc = entry.get("description", key)
        # Internal channel: explicit mapping > same-name assumption
        channel = "#" + ch_map.get(key, key)

        # Search HubSpot: try workspace key first, then description
        hub_name = hub_id = hub_domain = ""
        for search_term in ([key, desc] if desc.lower() != key.lower() else [key]):
            try:
                raw = hs.search_companies_by_name(search_term, limit=1)
                data = _json.loads(raw)
                results = data.get("results", [])
                if results:
                    props = results[0].get("properties", {})
                    hub_name = props.get("name", "?")
                    hub_id = results[0].get("id", "")
                    hub_domain = props.get("domain", "")
                    break
            except Exception:
                pass

        if hub_id:
            status = "✅"
            hub_str = f"*{hub_name}* (ID `{hub_id}`)"
            if hub_domain:
                hub_str += f"\n       domínio: {hub_domain}"
            hub_str += f"\n       🔗 {_hub_url(hub_id)}"
        else:
            status = "❌"
            hub_str = "não encontrado no HubSpot"

        rows.append(
            f"{status} *{desc}* (`{key}`)\n"
            f"   • Canal interno: `{channel}`\n"
            f"   • HubSpot: {hub_str}"
        )

    total = len(ws)
    matched = sum(1 for r in rows if r.startswith("✅"))
    header = (
        f"*Mapa de clientes — {matched}/{total} com match no HubSpot*\n"
        f"{'─' * 40}\n\n"
    )
    return header + "\n\n".join(rows)


def _fmt_ts(ts: str) -> str:
    """Convert Slack timestamp to DD/MM HH:MM."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(float(ts))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ts
