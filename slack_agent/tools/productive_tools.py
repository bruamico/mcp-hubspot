"""
Productive.io tools for the Claude agent.
Uses the Productive REST API (JSON:API spec).

Required env vars:
  PRODUCTIVE_TOKEN    - API token (X-Auth-Token header)
  PRODUCTIVE_ORG_ID   - Organization ID (X-Organization-Id header)
"""
import json
import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.productive.io/api/v2"


def _headers() -> dict:
    return {
        "X-Auth-Token": os.environ["PRODUCTIVE_TOKEN"],
        "X-Organization-Id": os.environ["PRODUCTIVE_ORG_ID"],
        "Content-Type": "application/vnd.api+json",
    }


async def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{BASE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers(), params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _post(path: str, body: dict) -> dict:
    url = f"{BASE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=_headers(), json=body) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _patch(path: str, body: dict) -> dict:
    url = f"{BASE_URL}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=_headers(), json=body) as resp:
            resp.raise_for_status()
            return await resp.json()


def _attr(item: dict) -> dict:
    """Merge id into attributes for easier access."""
    return {"id": item["id"], **item.get("attributes", {})}


def _list_attrs(data: list) -> list[dict]:
    return [_attr(item) for item in data]


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

PRODUCTIVE_TOOL_DEFINITIONS = [
    {
        "name": "productive_list_projects",
        "description": "Lista projetos no Productive. Pode filtrar por status (active/archived) e empresa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "archived", "all"],
                    "description": "Filtro de status: 'active' (padrão), 'archived' ou 'all'",
                },
                "company_id": {
                    "type": "string",
                    "description": "Filtrar por empresa (ID da company no Productive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de projetos (padrão: 20, max: 200)",
                },
            },
        },
    },
    {
        "name": "productive_get_project",
        "description": "Retorna detalhes de um projeto específico pelo ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "ID do projeto no Productive"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "productive_list_tasks",
        "description": "Lista tarefas no Productive. Pode filtrar por projeto, responsável, status e data de vencimento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filtrar por ID do projeto"},
                "assignee_id": {"type": "string", "description": "Filtrar por ID do responsável (person)"},
                "status": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Status das tarefas: 'open' (padrão), 'closed' ou 'all'",
                },
                "overdue": {
                    "type": "boolean",
                    "description": "Se true, retorna apenas tarefas com data de vencimento passada",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de tarefas (padrão: 30)",
                },
            },
        },
    },
    {
        "name": "productive_create_task",
        "description": "Cria uma nova tarefa no Productive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título da tarefa"},
                "project_id": {"type": "string", "description": "ID do projeto"},
                "assignee_id": {"type": "string", "description": "ID do responsável (person)"},
                "due_date": {"type": "string", "description": "Data de vencimento (YYYY-MM-DD)"},
                "description": {"type": "string", "description": "Descrição da tarefa"},
            },
            "required": ["title", "project_id"],
        },
    },
    {
        "name": "productive_update_task",
        "description": "Atualiza uma tarefa existente (status, responsável, data de vencimento, etc).",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID da tarefa"},
                "title": {"type": "string", "description": "Novo título"},
                "assignee_id": {"type": "string", "description": "Novo responsável (person ID)"},
                "due_date": {"type": "string", "description": "Nova data de vencimento (YYYY-MM-DD)"},
                "closed": {"type": "boolean", "description": "True para fechar a tarefa, False para reabrir"},
                "description": {"type": "string", "description": "Nova descrição"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "productive_list_people",
        "description": "Lista as pessoas (membros da equipe) no Productive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_deactivated": {
                    "type": "boolean",
                    "description": "Incluir pessoas desativadas (padrão: false)",
                },
                "limit": {"type": "integer", "description": "Número máximo (padrão: 50)"},
            },
        },
    },
    {
        "name": "productive_list_companies",
        "description": "Lista as empresas (clientes) cadastradas no Productive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Número máximo (padrão: 50)"},
            },
        },
    },
    {
        "name": "productive_list_time_entries",
        "description": "Lista registros de horas trabalhadas no Productive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filtrar por projeto"},
                "person_id": {"type": "string", "description": "Filtrar por pessoa"},
                "date_after": {"type": "string", "description": "Data inicial (YYYY-MM-DD)"},
                "date_before": {"type": "string", "description": "Data final (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "Número máximo (padrão: 30)"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

async def execute_productive_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "productive_list_projects":
            return await _list_projects(
                status=tool_input.get("status", "active"),
                company_id=tool_input.get("company_id"),
                limit=int(tool_input.get("limit", 20)),
            )
        elif tool_name == "productive_get_project":
            return await _get_project(tool_input["project_id"])
        elif tool_name == "productive_list_tasks":
            return await _list_tasks(
                project_id=tool_input.get("project_id"),
                assignee_id=tool_input.get("assignee_id"),
                status=tool_input.get("status", "open"),
                overdue=tool_input.get("overdue", False),
                limit=int(tool_input.get("limit", 30)),
            )
        elif tool_name == "productive_create_task":
            return await _create_task(tool_input)
        elif tool_name == "productive_update_task":
            return await _update_task(tool_input)
        elif tool_name == "productive_list_people":
            return await _list_people(
                include_deactivated=tool_input.get("include_deactivated", False),
                limit=int(tool_input.get("limit", 50)),
            )
        elif tool_name == "productive_list_companies":
            return await _list_companies(limit=int(tool_input.get("limit", 50)))
        elif tool_name == "productive_list_time_entries":
            return await _list_time_entries(
                project_id=tool_input.get("project_id"),
                person_id=tool_input.get("person_id"),
                date_after=tool_input.get("date_after"),
                date_before=tool_input.get("date_before"),
                limit=int(tool_input.get("limit", 30)),
            )
        else:
            return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("Productive tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

async def _list_projects(status: str, company_id: Optional[str], limit: int) -> str:
    params: dict[str, Any] = {"page[size]": min(limit, 200)}
    if status == "active":
        params["filter[archived]"] = "false"
    elif status == "archived":
        params["filter[archived]"] = "true"
    if company_id:
        params["filter[company_id]"] = company_id

    data = await _get("/projects", params)
    items = _list_attrs(data.get("data", []))
    total = data.get("meta", {}).get("total_count", "?")

    if not items:
        return "Nenhum projeto encontrado."

    lines = []
    for p in items:
        archived = " _(arquivado)_" if p.get("archived_at") else ""
        lines.append(f"• *{p.get('name', '?')}* — ID: `{p['id']}`{archived}")
    return f"Projetos ({len(items)} de {total}):\n" + "\n".join(lines)


async def _get_project(project_id: str) -> str:
    data = await _get(f"/projects/{project_id}")
    p = _attr(data["data"])
    archived = f"\n• Arquivado em: {p['archived_at'][:10]}" if p.get("archived_at") else ""
    return (
        f"*Projeto #{p.get('number', '?')}: {p.get('name', '?')}*\n"
        f"• ID: `{p['id']}`\n"
        f"• Última atividade: {(p.get('last_activity_at') or '')[:10]}"
        f"{archived}"
    )


async def _list_tasks(
    project_id: Optional[str],
    assignee_id: Optional[str],
    status: str,
    overdue: bool,
    limit: int,
) -> str:
    import datetime

    params: dict[str, Any] = {"page[size]": min(limit, 200)}
    if status == "open":
        params["filter[closed]"] = "false"
    elif status == "closed":
        params["filter[closed]"] = "true"
    if project_id:
        params["filter[project_id]"] = project_id
    if assignee_id:
        params["filter[assignee_id]"] = assignee_id
    if overdue:
        today = datetime.date.today().isoformat()
        params["filter[due_date_before]"] = today
        params["filter[closed]"] = "false"

    data = await _get("/tasks", params)
    items = _list_attrs(data.get("data", []))
    total = data.get("meta", {}).get("total_count", "?")

    if not items:
        return "Nenhuma tarefa encontrada com esses filtros."

    lines = []
    for t in items:
        due = f" | vence {t['due_date']}" if t.get("due_date") else ""
        closed = " ✅" if t.get("closed") else ""
        lines.append(f"• *{t.get('title', '?')}*{closed}{due} — ID: `{t['id']}`")
    return f"Tarefas ({len(items)} de {total}):\n" + "\n".join(lines)


async def _create_task(tool_input: dict) -> str:
    attributes: dict[str, Any] = {"title": tool_input["title"]}
    if tool_input.get("due_date"):
        attributes["due_date"] = tool_input["due_date"]
    if tool_input.get("description"):
        attributes["description"] = tool_input["description"]

    relationships: dict[str, Any] = {
        "project": {"data": {"type": "projects", "id": tool_input["project_id"]}}
    }
    if tool_input.get("assignee_id"):
        relationships["assignee"] = {"data": {"type": "people", "id": tool_input["assignee_id"]}}

    body = {"data": {"type": "tasks", "attributes": attributes, "relationships": relationships}}
    data = await _post("/tasks", body)
    t = _attr(data["data"])
    return (
        f"Tarefa criada com sucesso!\n"
        f"• *{t.get('title')}* — ID: `{t['id']}`\n"
        f"• Vencimento: {t.get('due_date', 'não definido')}"
    )


async def _update_task(tool_input: dict) -> str:
    task_id = tool_input["task_id"]
    attributes: dict[str, Any] = {}
    relationships: dict[str, Any] = {}

    if "title" in tool_input:
        attributes["title"] = tool_input["title"]
    if "due_date" in tool_input:
        attributes["due_date"] = tool_input["due_date"]
    if "closed" in tool_input:
        attributes["closed"] = tool_input["closed"]
    if "description" in tool_input:
        attributes["description"] = tool_input["description"]
    if "assignee_id" in tool_input:
        relationships["assignee"] = {"data": {"type": "people", "id": tool_input["assignee_id"]}}

    body: dict[str, Any] = {"data": {"type": "tasks", "id": task_id, "attributes": attributes}}
    if relationships:
        body["data"]["relationships"] = relationships

    data = await _patch(f"/tasks/{task_id}", body)
    t = _attr(data["data"])
    status = "fechada ✅" if t.get("closed") else "aberta"
    return (
        f"Tarefa atualizada!\n"
        f"• *{t.get('title')}* — ID: `{t['id']}`\n"
        f"• Status: {status} | Vencimento: {t.get('due_date', 'não definido')}"
    )


async def _list_people(include_deactivated: bool, limit: int) -> str:
    params: dict[str, Any] = {"page[size]": min(limit, 200)}
    if not include_deactivated:
        params["filter[deactivated]"] = "false"

    data = await _get("/people", params)
    items = _list_attrs(data.get("data", []))
    total = data.get("meta", {}).get("total_count", "?")

    if not items:
        return "Nenhuma pessoa encontrada."

    lines = []
    for p in items:
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        title = f" — {p['title']}" if p.get("title") else ""
        lines.append(f"• *{name}*{title} | `{p.get('email', '')}` — ID: `{p['id']}`")
    return f"Pessoas ({len(items)} de {total}):\n" + "\n".join(lines)


async def _list_companies(limit: int) -> str:
    params: dict[str, Any] = {"page[size]": min(limit, 200), "filter[archived]": "false"}
    data = await _get("/companies", params)
    items = _list_attrs(data.get("data", []))
    total = data.get("meta", {}).get("total_count", "?")

    if not items:
        return "Nenhuma empresa encontrada."

    lines = []
    for c in items:
        code = f" `{c['company_code']}`" if c.get("company_code") else ""
        lines.append(f"• *{c.get('name', '?')}*{code} — ID: `{c['id']}`")
    return f"Empresas ({len(items)} de {total}):\n" + "\n".join(lines)


async def _list_time_entries(
    project_id: Optional[str],
    person_id: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    limit: int,
) -> str:
    params: dict[str, Any] = {"page[size]": min(limit, 200)}
    if project_id:
        params["filter[project_id]"] = project_id
    if person_id:
        params["filter[person_id]"] = person_id
    if date_after:
        params["filter[after]"] = date_after
    if date_before:
        params["filter[before]"] = date_before

    data = await _get("/time_entries", params)
    items = _list_attrs(data.get("data", []))
    total = data.get("meta", {}).get("total_count", "?")

    if not items:
        return "Nenhum registro de horas encontrado."

    lines = []
    for e in items:
        mins = e.get("time", 0) or 0
        hours = f"{mins // 60}h{mins % 60:02d}m" if mins else "?"
        date = (e.get("date") or "")[:10]
        note = f" — {e['note'][:60]}" if e.get("note") else ""
        lines.append(f"• {date} | {hours}{note} — ID: `{e['id']}`")
    return f"Registros de horas ({len(items)} de {total}):\n" + "\n".join(lines)
