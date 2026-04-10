"""
Google Calendar tools for the Claude agent.
Reads events from the user's Google Calendar using stored OAuth tokens.
"""
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

GCAL_TOOL_DEFINITIONS = [
    {
        "name": "gcal_list_events",
        "description": (
            "Lista eventos da agenda Google Calendar. "
            "Útil para ver reuniões agendadas com clientes, identificar quem participa e preparar contexto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "Quantos dias à frente buscar (padrão: 7)",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Quantos dias atrás buscar (padrão: 0 — só futuros)",
                },
                "query": {
                    "type": "string",
                    "description": "Filtrar eventos por palavra-chave no título",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Número máximo de eventos (padrão: 20)",
                },
            },
        },
    },
]


async def execute_gcal_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "gcal_list_events":
        return await _list_events(
            days_ahead=tool_input.get("days_ahead", 7),
            days_back=tool_input.get("days_back", 0),
            query=tool_input.get("query"),
            max_results=tool_input.get("max_results", 20),
        )
    return f"Ferramenta desconhecida: {tool_name}"


async def _get_valid_token() -> Optional[str]:
    """Return a valid access token, refreshing if expired."""
    from ..models.database import get_oauth_token, save_oauth_token
    row = await get_oauth_token("google_calendar")
    if not row:
        return None

    # Refresh if expired (with 60s buffer)
    if row["expires_at"] and int(row["expires_at"]) < time.time() + 60:
        refreshed = await _refresh_token(row["refresh_token"])
        if refreshed:
            return refreshed
        return None

    return row["access_token"]


async def _refresh_token(refresh_token: str) -> Optional[str]:
    """Use refresh_token to get a new access_token."""
    import aiohttp
    from ..models.database import save_oauth_token

    async with aiohttp.ClientSession() as session:
        async with session.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        }) as resp:
            if resp.status != 200:
                logger.error("Google token refresh failed: %s", await resp.text())
                return None
            data = await resp.json()

    await save_oauth_token(
        service="google_calendar",
        access_token=data["access_token"],
        refresh_token=refresh_token,
        expires_in=data.get("expires_in", 3600),
        scope=data.get("scope", ""),
    )
    return data["access_token"]


async def _list_events(
    days_ahead: int = 7,
    days_back: int = 0,
    query: Optional[str] = None,
    max_results: int = 20,
) -> str:
    import aiohttp
    import datetime

    token = await _get_valid_token()
    if not token:
        return (
            "O Google Calendar não está autorizado. "
            "Acesse https://tropical-bot.fly.dev/oauth/google no navegador para conectar."
        )

    now = datetime.datetime.utcnow()
    time_min = (now - datetime.timedelta(days=days_back)).isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=days_ahead)).isoformat() + "Z"

    params = {
        "calendarId": "primary",
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": str(max_results),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if query:
        params["q"] = query

    from urllib.parse import urlencode
    url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{urlencode(params)}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            if resp.status == 401:
                return (
                    "Token do Google Calendar expirado ou revogado. "
                    "Reautorize em https://tropical-bot.fly.dev/oauth/google"
                )
            if resp.status != 200:
                return f"⚠️ERRO Google Calendar: status {resp.status} — {await resp.text()}"
            data = await resp.json()

    events = data.get("items", [])
    if not events:
        return f"Nenhum evento encontrado nos próximos {days_ahead} dias."

    lines = []
    for e in events:
        start = e.get("start", {})
        dt = start.get("dateTime") or start.get("date") or ""
        if "T" in dt:
            # Format datetime
            try:
                d = datetime.datetime.fromisoformat(dt.replace("Z", "+00:00"))
                dt_str = d.strftime("%d/%m %H:%M")
            except Exception:
                dt_str = dt[:16]
        else:
            dt_str = dt  # all-day event

        title = e.get("summary", "(sem título)")
        attendees = e.get("attendees", [])
        attendee_names = [
            a.get("displayName") or a.get("email", "")
            for a in attendees
            if not a.get("self")
        ]
        attendees_str = f" | {', '.join(attendee_names[:4])}" if attendee_names else ""
        location = f" @ {e['location'][:40]}" if e.get("location") else ""
        lines.append(f"• {dt_str} — *{title}*{attendees_str}{location}")

    header = f"Agenda — próximos {days_ahead}d ({len(events)} eventos):\n"
    return header + "\n".join(lines)
