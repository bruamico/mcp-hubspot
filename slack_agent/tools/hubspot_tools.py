"""
HubSpot tool definitions for the Claude agent.
Each tool has:
  - A JSON schema (sent to Claude in the `tools` parameter)
  - An executor function (called when Claude requests the tool)

The executor calls HubSpotClient methods directly, avoiding the MCP
handler layer (and its FAISS/embedding dependencies).
"""
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _make_hubspot_client():
    """Lazy-import HubSpotClient to avoid loading it at module import time."""
    from mcp_server_hubspot.hubspot_client import HubSpotClient

    token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN")
    return HubSpotClient(access_token=token)


# Singleton — created on first tool call
_hs: Any = None


def _hs_client():
    global _hs
    if _hs is None:
        _hs = _make_hubspot_client()
    return _hs


# ---------------------------------------------------------------------------
# Tool definitions (JSON schema for Claude)
# ---------------------------------------------------------------------------

HUBSPOT_TOOL_DEFINITIONS = [
    {
        "name": "hubspot_get_active_contacts",
        "description": "Busca os contatos mais recentemente ativos no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de contatos (padrão: 10)",
                }
            },
        },
    },
    {
        "name": "hubspot_get_contact",
        "description": "Busca um contato específico pelo seu ID no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "ID do contato no HubSpot",
                },
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista opcional de propriedades a retornar",
                },
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "hubspot_create_contact",
        "description": "Cria um novo contato no HubSpot (com verificação de duplicata).",
        "input_schema": {
            "type": "object",
            "properties": {
                "firstname": {"type": "string", "description": "Primeiro nome"},
                "lastname": {"type": "string", "description": "Sobrenome"},
                "email": {"type": "string", "description": "E-mail"},
                "properties": {
                    "type": "object",
                    "description": "Propriedades adicionais (empresa, telefone etc.)",
                },
            },
            "required": ["firstname", "lastname"],
        },
    },
    {
        "name": "hubspot_update_contact",
        "description": "Atualiza propriedades de um contato existente no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "ID do contato"},
                "properties": {
                    "type": "object",
                    "description": "Propriedades a atualizar",
                },
            },
            "required": ["contact_id", "properties"],
        },
    },
    {
        "name": "hubspot_get_active_companies",
        "description": "Busca as empresas mais recentemente ativas no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de empresas (padrão: 10)",
                }
            },
        },
    },
    {
        "name": "hubspot_get_company",
        "description": "Busca uma empresa específica pelo seu ID no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"},
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista opcional de propriedades a retornar",
                },
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "hubspot_get_company_activity",
        "description": "Retorna o histórico de atividades de uma empresa no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"}
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "hubspot_create_company",
        "description": "Cria uma nova empresa no HubSpot (com verificação de duplicata).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome da empresa"},
                "properties": {
                    "type": "object",
                    "description": "Propriedades adicionais",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "hubspot_update_company",
        "description": "Atualiza propriedades de uma empresa existente no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"},
                "properties": {
                    "type": "object",
                    "description": "Propriedades a atualizar",
                },
            },
            "required": ["company_id", "properties"],
        },
    },
    {
        "name": "hubspot_get_tickets",
        "description": "Busca tickets no HubSpot. Use criteria='default' para tickets abertos/recentes e criteria='Closed' para encerrados.",
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "string",
                    "enum": ["default", "Closed"],
                    "description": "'default' = abertos/recentes, 'Closed' = encerrados",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de tickets (padrão: 20)",
                },
            },
        },
    },
    {
        "name": "hubspot_get_ticket_threads",
        "description": "Retorna as threads de conversa de um ticket específico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "ID do ticket"}
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "hubspot_get_recent_conversations",
        "description": "Busca as conversas/emails recentes no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de threads (padrão: 10)",
                }
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

async def execute_hubspot_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a HubSpot tool and return the result as a string."""
    hs = _hs_client()
    try:
        if tool_name == "hubspot_get_active_contacts":
            limit = tool_input.get("limit", 10)
            return hs.get_recent_contacts(limit=int(limit))

        elif tool_name == "hubspot_get_contact":
            return hs.get_contact_by_id(
                tool_input["contact_id"],
                tool_input.get("properties"),
            )

        elif tool_name == "hubspot_create_contact":
            # Delegate to handler for duplicate-check logic
            from mcp_server_hubspot.handlers.contact_handler import ContactHandler
            handler = _get_contact_handler()
            result = handler.create_contact(tool_input)
            return result[0].text if result else ""

        elif tool_name == "hubspot_update_contact":
            return hs.update_contact(
                tool_input["contact_id"], tool_input["properties"]
            )

        elif tool_name == "hubspot_get_active_companies":
            limit = tool_input.get("limit", 10)
            return hs.get_recent_companies(limit=int(limit))

        elif tool_name == "hubspot_get_company":
            return hs.get_company_by_id(
                tool_input["company_id"],
                tool_input.get("properties"),
            )

        elif tool_name == "hubspot_get_company_activity":
            return hs.get_company_activity(tool_input["company_id"])

        elif tool_name == "hubspot_create_company":
            handler = _get_company_handler()
            result = handler.create_company(tool_input)
            return result[0].text if result else ""

        elif tool_name == "hubspot_update_company":
            return hs.update_company(
                tool_input["company_id"], tool_input["properties"]
            )

        elif tool_name == "hubspot_get_tickets":
            criteria = tool_input.get("criteria", "default")
            limit = tool_input.get("limit", 20)
            result = hs.get_tickets(criteria=criteria, limit=int(limit))
            return json.dumps(result) if isinstance(result, dict) else str(result)

        elif tool_name == "hubspot_get_ticket_threads":
            result = hs.get_ticket_conversation_threads(tool_input["ticket_id"])
            return json.dumps(result) if isinstance(result, dict) else str(result)

        elif tool_name == "hubspot_get_recent_conversations":
            limit = tool_input.get("limit", 10)
            result = hs.get_recent_conversations(limit=int(limit))
            return json.dumps(result) if isinstance(result, dict) else str(result)

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("HubSpot tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


# Lightweight handler instances for create operations (need duplicate-check logic)
_contact_handler = None
_company_handler = None


def _get_contact_handler():
    global _contact_handler
    if _contact_handler is None:
        from mcp_server_hubspot.handlers.contact_handler import ContactHandler
        _contact_handler = ContactHandler(_hs_client(), None, None)
    return _contact_handler


def _get_company_handler():
    global _company_handler
    if _company_handler is None:
        from mcp_server_hubspot.handlers.company_handler import CompanyHandler
        _company_handler = CompanyHandler(_hs_client(), None, None)
    return _company_handler
