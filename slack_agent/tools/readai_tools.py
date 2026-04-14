"""
Read.ai tool definitions for the Claude agent.
Queries the SQLite database populated by the webhook handler.
"""
import json
import logging

from ..models.database import get_recent_meetings, search_meetings, get_meetings_in_window

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
                    "description": "Número máximo de reuniões (padrão: 10)",
                }
            },
        },
    },
    {
        "name": "readai_search_meetings",
        "description": (
            "Busca reuniões do Read.ai pelo conteúdo do título, resumo, action items ou participantes. "
            "Use para encontrar reuniões de um cliente específico ou sobre um tema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Termo de busca (nome do cliente, tema, participante...)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de resultados (padrão: 20)",
                },
                "since": {
                    "type": "string",
                    "description": "Filtrar reuniões a partir desta data (YYYY-MM-DD). Opcional.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "readai_meetings_in_window",
        "description": (
            "Lista TODAS as reuniões registradas no Read.ai dentro de um período. "
            "Use para diagnóstico (verificar o que está no banco) ou quando não souber o nome exato do cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Data inicial no formato YYYY-MM-DD (obrigatório)",
                },
                "until": {
                    "type": "string",
                    "description": "Data final YYYY-MM-DD (opcional, padrão: hoje)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de resultados (padrão: 50)",
                },
            },
            "required": ["since"],
        },
    },
]


async def execute_readai_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a Read.ai tool and return the result as JSON string."""
    try:
        if tool_name == "readai_get_recent_meetings":
            limit = tool_input.get("limit", 10)
            rows = await get_recent_meetings(limit=int(limit))
            if not rows:
                return "Nenhuma reunião encontrada no banco de dados."
            return json.dumps(rows, ensure_ascii=False, indent=2)

        elif tool_name == "readai_search_meetings":
            query = tool_input["query"]
            limit = tool_input.get("limit", 20)
            since = tool_input.get("since") or None
            rows = await search_meetings(query=query, limit=int(limit), since_iso=since)
            if not rows:
                return f"Nenhuma reunião encontrada para '{query}'."
            return json.dumps(rows, ensure_ascii=False, indent=2)

        elif tool_name == "readai_meetings_in_window":
            since = tool_input["since"]
            until = tool_input.get("until") or None
            limit = tool_input.get("limit", 50)
            rows = await get_meetings_in_window(since_iso=since, until_iso=until, limit=int(limit))
            if not rows:
                return f"Nenhuma reunião encontrada no período {since} → {until or 'hoje'}."
            return json.dumps(rows, ensure_ascii=False, indent=2)

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("Read.ai tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"
