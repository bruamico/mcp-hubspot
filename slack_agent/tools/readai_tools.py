"""
Read.ai tool definitions for the Claude agent.
Queries the SQLite database populated by the webhook handler.
"""
import json
import logging

from ..models.database import get_recent_meetings, search_meetings

logger = logging.getLogger(__name__)

READAI_TOOL_DEFINITIONS = [
    {
        "name": "readai_get_recent_meetings",
        "description": (
            "Retorna as reuniões mais recentes registradas pelo Read.ai, "
            "incluindo resumo, participantes e action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de reuniões (padrão: 5)",
                }
            },
        },
    },
    {
        "name": "readai_search_meetings",
        "description": (
            "Busca reuniões do Read.ai pelo conteúdo do título, resumo ou action items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termo de busca",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de resultados (padrão: 5)",
                },
            },
            "required": ["query"],
        },
    },
]


async def execute_readai_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a Read.ai tool and return the result as JSON string."""
    try:
        if tool_name == "readai_get_recent_meetings":
            limit = tool_input.get("limit", 5)
            rows = await get_recent_meetings(limit=int(limit))
            if not rows:
                return "Nenhuma reunião encontrada no banco de dados."
            return json.dumps(rows, ensure_ascii=False, indent=2)

        elif tool_name == "readai_search_meetings":
            query = tool_input["query"]
            limit = tool_input.get("limit", 5)
            rows = await search_meetings(query=query, limit=int(limit))
            if not rows:
                return f"Nenhuma reunião encontrada para '{query}'."
            return json.dumps(rows, ensure_ascii=False, indent=2)

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("Read.ai tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"
