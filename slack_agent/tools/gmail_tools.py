"""
Gmail tools for the Claude agent.
Reads emails using stored Google OAuth tokens (gmail.readonly scope).
"""
import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

GMAIL_TOOL_DEFINITIONS = [
    {
        "name": "gmail_list_emails",
        "description": (
            "Lista emails recentes da caixa de entrada. "
            "Pode filtrar por remetente, assunto ou palavra-chave."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Filtro Gmail (ex: 'from:galena.com.br', 'subject:reunião', "
                        "'is:unread', 'after:2024/01/01'). Deixe vazio para emails recentes."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Número máximo de emails (padrão: 10)",
                },
            },
        },
    },
    {
        "name": "gmail_read_email",
        "description": "Lê o conteúdo completo de um email pelo ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "ID do email (obtido via gmail_list_emails)",
                },
            },
            "required": ["message_id"],
        },
    },
]


async def execute_gmail_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "gmail_list_emails":
        return await _list_emails(
            query=tool_input.get("query", ""),
            max_results=int(tool_input.get("max_results", 10)),
        )
    elif tool_name == "gmail_read_email":
        return await _read_email(tool_input["message_id"])
    return f"Ferramenta desconhecida: {tool_name}"


async def _get_token() -> Optional[str]:
    from .gcal_tools import _get_valid_token
    return await _get_valid_token()


def _not_authorized() -> str:
    return (
        "Gmail não autorizado. "
        "Acesse https://tropical-bot.fly.dev/oauth/google no navegador para conectar."
    )


async def _list_emails(query: str = "", max_results: int = 10) -> str:
    import aiohttp

    token = await _get_token()
    if not token:
        return _not_authorized()

    from urllib.parse import urlencode
    params = {"maxResults": max_results, "q": query or "in:inbox"}
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?{urlencode(params)}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status == 401:
                return _not_authorized()
            if resp.status != 200:
                return f"⚠️ERRO Gmail: {resp.status} — {await resp.text()}"
            data = await resp.json()

    messages = data.get("messages", [])
    if not messages:
        return "Nenhum email encontrado."

    # Fetch metadata for each message in parallel
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}

        async def _meta(msg_id: str) -> dict:
            u = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
            async with session.get(u, headers=headers) as r:
                return await r.json() if r.status == 200 else {}

        import asyncio
        metas = await asyncio.gather(*[_meta(m["id"]) for m in messages])

    lines = []
    for meta in metas:
        if not meta:
            continue
        headers_list = meta.get("payload", {}).get("headers", [])
        h = {h["name"]: h["value"] for h in headers_list}
        snippet = meta.get("snippet", "")[:120]
        unread = "🔵 " if "UNREAD" in meta.get("labelIds", []) else ""
        lines.append(
            f"{unread}*{h.get('Subject', '(sem assunto)')}*\n"
            f"  De: {h.get('From', '?')} | {h.get('Date', '')[:16]}\n"
            f"  {snippet}"
        )
        lines.append(f"  ID: `{meta['id']}`")

    return f"Emails ({len(lines) // 2}):\n\n" + "\n\n".join(
        "\n".join(lines[i:i+2]) for i in range(0, len(lines), 2)
    )


async def _read_email(message_id: str) -> str:
    import aiohttp

    token = await _get_token()
    if not token:
        return _not_authorized()

    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status == 401:
                return _not_authorized()
            if resp.status != 200:
                return f"⚠️ERRO Gmail: {resp.status}"
            data = await resp.json()

    headers_list = data.get("payload", {}).get("headers", [])
    h = {hh["name"]: hh["value"] for hh in headers_list}

    # Extract body text
    body = _extract_body(data.get("payload", {}))

    return (
        f"*{h.get('Subject', '(sem assunto)')}*\n"
        f"De: {h.get('From', '?')}\n"
        f"Para: {h.get('To', '?')}\n"
        f"Data: {h.get('Date', '?')}\n\n"
        f"{body[:3000]}"
    )


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result
    # Fallback: try HTML
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            # Strip tags minimally
            import re
            return re.sub(r"<[^>]+>", " ", html)[:3000]
    return ""
