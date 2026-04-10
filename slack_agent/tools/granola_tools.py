"""
Granola meeting notes tools for the Claude agent.
Connects to the Granola remote MCP server at https://mcp.granola.ai/mcp
using OAuth 2.0 tokens stored in SQLite.

OAuth setup: visit https://tropical-bot.fly.dev/oauth/granola on your browser.

Tool strategy:
  - granola_list_available_tools  → calls tools/list on the MCP server so Claude
                                     learns the exact names this server exposes
  - granola_call_tool             → generic pass-through: call any tool by name
  - granola_list_notes            → convenience wrapper (tries common names)
  - granola_search_notes          → convenience wrapper with multi-name fallback
  - granola_get_note              → convenience wrapper
"""
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from ..models.database import get_oauth_token, save_oauth_token

logger = logging.getLogger(__name__)

MCP_URL = "https://mcp.granola.ai/mcp"
TOKEN_URL = "https://mcp-auth.granola.ai/oauth2/token"
CLIENT_ID = "client_01KNS9F4SQRJHEXA3HCH4N8VBQ"
SERVICE = "granola"

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

async def _get_valid_token() -> Optional[str]:
    """Return a valid access token, refreshing if expired."""
    token_data = await get_oauth_token(SERVICE)
    if not token_data:
        return None

    # Refresh if expired or expiring in next 60s
    if token_data["expires_at"] - 60 < time.time():
        refreshed = await _refresh_token(token_data["refresh_token"])
        if refreshed:
            return refreshed
        return None

    return token_data["access_token"]


async def _refresh_token(refresh_token: str) -> Optional[str]:
    """Use refresh_token to get new access_token. Returns new access_token or None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            }) as resp:
                if resp.status != 200:
                    logger.error("Token refresh failed: %s", await resp.text())
                    return None
                data = await resp.json()
                await save_oauth_token(
                    SERVICE,
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token", refresh_token),
                    expires_in=data.get("expires_in", 3600),
                    scope=data.get("scope", ""),
                )
                return data["access_token"]
    except Exception as exc:
        logger.error("Token refresh error: %s", exc)
        return None


def _not_authed() -> str:
    return (
        "Granola não está autenticado. "
        "Acesse https://tropical-bot.fly.dev/oauth/granola no seu navegador para autorizar."
    )


# ---------------------------------------------------------------------------
# MCP client helper
# ---------------------------------------------------------------------------

_mcp_session_id: Optional[str] = None
_mcp_request_id: int = 0


async def _mcp_call(method: str, params: dict, token: str) -> Any:
    """Make a JSON-RPC call to the Granola MCP server."""
    global _mcp_session_id, _mcp_request_id
    _mcp_request_id += 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2024-11-05",
    }
    if _mcp_session_id:
        headers["MCP-Session-Id"] = _mcp_session_id

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": _mcp_request_id,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(MCP_URL, headers=headers, json=payload) as resp:
            if resp.status == 401:
                raise PermissionError("Token expirado ou inválido. Reautorize em /oauth/granola")

            # Capture session ID from first response
            if not _mcp_session_id and resp.headers.get("MCP-Session-Id"):
                _mcp_session_id = resp.headers["MCP-Session-Id"]

            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                text = await resp.text()
                for line in text.splitlines():
                    if line.startswith("data: "):
                        msg = json.loads(line[6:])
                        if "result" in msg:
                            return msg["result"]
                        if "error" in msg:
                            raise RuntimeError(msg["error"].get("message", "MCP error"))
                return None
            else:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(data["error"].get("message", "MCP error"))
                return data.get("result")


async def _ensure_initialized(token: str) -> None:
    """Initialize MCP session if not already done."""
    global _mcp_session_id
    if _mcp_session_id:
        return
    await _mcp_call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "Tropical Bot", "version": "1.0"},
    }, token)


async def _raw_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call any tool on the Granola MCP server, with one session-reset retry."""
    global _mcp_session_id

    token = await _get_valid_token()
    if not token:
        return _not_authed()

    for attempt in range(2):
        try:
            await _ensure_initialized(token)
            result = await _mcp_call("tools/call", {"name": tool_name, "arguments": arguments}, token)
            if result and "content" in result:
                parts = [c.get("text", "") for c in result["content"] if c.get("type") == "text"]
                return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
            return json.dumps(result, ensure_ascii=False, default=str)
        except PermissionError as exc:
            _mcp_session_id = None
            return str(exc)
        except Exception as exc:
            _mcp_session_id = None
            if attempt == 0:
                logger.warning("Granola MCP call %s failed (attempt 1), retrying: %s", tool_name, exc)
                continue
            logger.error("Granola MCP call %s failed: %s", tool_name, exc)
            return f"Erro ao chamar Granola ({tool_name}): {exc}"

    return f"Erro ao chamar Granola ({tool_name}): falha após retry"


async def _list_available_tools() -> str:
    """Call tools/list on the Granola MCP server and return a summary."""
    global _mcp_session_id

    token = await _get_valid_token()
    if not token:
        return _not_authed()

    try:
        await _ensure_initialized(token)
        result = await _mcp_call("tools/list", {}, token)
        if not result:
            return "Sem resultado do servidor Granola."
        tools = result.get("tools", [])
        if not tools:
            return "Nenhuma ferramenta disponível no servidor Granola."
        lines = []
        for t in tools:
            desc = t.get("description", "")[:100]
            lines.append(f"• `{t['name']}` — {desc}")
        return f"Ferramentas disponíveis no Granola MCP ({len(tools)}):\n" + "\n".join(lines)
    except PermissionError as exc:
        _mcp_session_id = None
        return str(exc)
    except Exception as exc:
        _mcp_session_id = None
        logger.error("Granola tools/list failed: %s", exc)
        return f"Erro ao listar ferramentas do Granola: {exc}"


async def _search_notes_with_fallback(query: str) -> str:
    """Try several common search tool names before giving up."""
    # Candidates in order of likelihood
    candidates = [
        ("search_notes", {"query": query}),
        ("search_documents", {"query": query}),
        ("search", {"query": query}),
        ("search_meetings", {"query": query}),
    ]
    for tool_name, args in candidates:
        result = await _raw_mcp_tool(tool_name, args)
        # If the error is specifically "not found", try next candidate
        if "not found" in result.lower() or "unknown tool" in result.lower():
            logger.info("Granola tool '%s' not found, trying next candidate", tool_name)
            continue
        return result

    # All search tools failed — fall back to listing all notes and hint user
    logger.warning("All Granola search candidates failed; falling back to list_notes")
    list_result = await _raw_mcp_tool("list_notes", {"limit": 20})
    if "not found" in list_result.lower():
        list_result = await _raw_mcp_tool("list_documents", {"limit": 20})
    return (
        f"[Busca indisponível no Granola — listando notas recentes para você filtrar]\n\n"
        f"{list_result}"
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GRANOLA_TOOL_DEFINITIONS = [
    {
        "name": "granola_list_available_tools",
        "description": (
            "Lista as ferramentas disponíveis no servidor MCP do Granola. "
            "Use PRIMEIRO quando outros tools do Granola retornarem 'not found' — "
            "isso mostra os nomes exatos que o servidor aceita."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "granola_call_tool",
        "description": (
            "Chama qualquer ferramenta do servidor MCP do Granola diretamente pelo nome. "
            "Use após granola_list_available_tools para chamar a ferramenta certa com os argumentos corretos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Nome exato da ferramenta no servidor Granola (ex: 'list_notes', 'get_document')",
                },
                "arguments": {
                    "type": "object",
                    "description": "Argumentos para a ferramenta (objeto JSON)",
                },
            },
            "required": ["tool_name"],
        },
    },
    {
        "name": "granola_list_notes",
        "description": (
            "Lista as notas de reuniões recentes do Granola, com título, data e resumo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de notas a retornar (padrão: 10)",
                },
            },
        },
    },
    {
        "name": "granola_search_notes",
        "description": "Busca notas de reuniões no Granola por palavra-chave.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termo de busca (nome de cliente, assunto, participante, etc.)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "granola_get_note",
        "description": "Retorna o conteúdo completo de uma nota de reunião específica do Granola.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "ID da nota no Granola",
                },
            },
            "required": ["note_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

async def execute_granola_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "granola_list_available_tools":
            return await _list_available_tools()

        elif tool_name == "granola_call_tool":
            return await _raw_mcp_tool(
                tool_input["tool_name"],
                tool_input.get("arguments") or {},
            )

        elif tool_name == "granola_list_notes":
            # Try common list tool names
            for name in ("list_notes", "list_documents", "get_notes"):
                result = await _raw_mcp_tool(name, {"limit": tool_input.get("limit", 10)})
                if "not found" not in result.lower() and "unknown tool" not in result.lower():
                    return result
            return await _list_available_tools()

        elif tool_name == "granola_search_notes":
            return await _search_notes_with_fallback(tool_input["query"])

        elif tool_name == "granola_get_note":
            # Try common get tool names
            note_id = tool_input["note_id"]
            for name in ("get_note", "get_document", "get_notes"):
                result = await _raw_mcp_tool(name, {"id": note_id})
                if "not found" not in result.lower() and "unknown tool" not in result.lower():
                    return result
            return await _raw_mcp_tool("get_note", {"note_id": note_id})

        else:
            return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("Granola tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"
