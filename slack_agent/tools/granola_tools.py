"""
Granola meeting notes tools for the Claude agent.
Connects to the Granola remote MCP server at https://mcp.granola.ai/mcp
using OAuth 2.0 tokens stored in SQLite.

OAuth setup: visit https://tropical-bot.fly.dev/oauth/granola on your browser.
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
                # Parse SSE stream
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


async def _call_granola_tool(tool_name: str, arguments: dict) -> str:
    """Call a tool on the Granola MCP server."""
    token = await _get_valid_token()
    if not token:
        return (
            "Granola não está autenticado. "
            "Acesse https://tropical-bot.fly.dev/oauth/granola no seu navegador para autorizar."
        )
    try:
        await _ensure_initialized(token)
        result = await _mcp_call("tools/call", {"name": tool_name, "arguments": arguments}, token)
        if result and "content" in result:
            parts = [c.get("text", "") for c in result["content"] if c.get("type") == "text"]
            return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False, default=str)
    except PermissionError as exc:
        global _mcp_session_id
        _mcp_session_id = None  # Reset session on auth failure
        return str(exc)
    except Exception as exc:
        logger.error("Granola MCP call %s failed: %s", tool_name, exc)
        return f"Erro ao chamar Granola ({tool_name}): {exc}"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GRANOLA_TOOL_DEFINITIONS = [
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
        if tool_name == "granola_list_notes":
            return await _call_granola_tool("list_notes", {
                "limit": tool_input.get("limit", 10),
            })
        elif tool_name == "granola_search_notes":
            return await _call_granola_tool("search_notes", {
                "query": tool_input["query"],
            })
        elif tool_name == "granola_get_note":
            return await _call_granola_tool("get_note", {
                "note_id": tool_input["note_id"],
            })
        else:
            return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("Granola tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"
