"""
Persistent memory tools for the Claude agent.

Lightweight mempalace-style memory stored in SQLite.
Organized by client_key (wing) + topic (room).

Wings  → client_key: 'galena', 'sympla', '' (global/equipe)
Rooms  → topic:      'decisões', 'preferências', 'acionáveis', 'contexto', 'reuniões'
"""
import json
import logging

from ..models.database import save_memory, recall_memories, list_memory_topics, delete_memory

logger = logging.getLogger(__name__)

MEMORY_TOOL_DEFINITIONS = [
    {
        "name": "memory_recall",
        "description": (
            "Busca memórias persistentes de sessões anteriores sobre clientes, "
            "decisões, preferências, acionáveis e contexto importante. "
            "Use no início de conversas sobre um cliente específico para recuperar "
            "informações relevantes de interações passadas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": (
                        "Chave do cliente (ex: 'galena', 'sympla'). "
                        "Use '' ou omita para memória global da equipe."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Tópico específico para filtrar: 'decisões', 'preferências', "
                        "'acionáveis', 'contexto', 'reuniões'. Omita para buscar em todos."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Texto livre para buscar dentro das memórias (busca parcial).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de memórias a retornar (padrão: 10).",
                },
            },
        },
    },
    {
        "name": "memory_save",
        "description": (
            "Salva uma informação importante na memória persistente para uso futuro. "
            "Use para registrar: decisões tomadas, preferências do cliente, "
            "compromissos assumidos, contexto crítico, padrões observados. "
            "Seja específico e conciso — escreva como uma nota que será lida meses depois."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": (
                        "Chave do cliente (ex: 'galena'). "
                        "Use '' para informações globais da equipe."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Categoria da memória: 'decisões', 'preferências', "
                        "'acionáveis', 'contexto', 'reuniões', 'alertas'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "O que salvar. Seja específico: quem disse, o quê, quando. "
                        "Ex: 'Bruno (09/04): decidiu pausar onboarding até maio por budget freeze.'"
                    ),
                },
                "source": {
                    "type": "string",
                    "description": "Origem: 'slack', 'readai', 'hubspot', 'manual', 'agent'.",
                },
            },
            "required": ["client_key", "topic", "content"],
        },
    },
    {
        "name": "memory_list_topics",
        "description": (
            "Lista os tópicos de memória existentes para um cliente. "
            "Útil para descobrir o que já foi registrado antes de buscar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "Chave do cliente. Use '' para memória global.",
                },
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Remove uma memória específica pelo ID (use quando estiver desatualizada ou incorreta).",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "ID da memória a remover (visível no resultado do memory_recall).",
                },
            },
            "required": ["memory_id"],
        },
    },
]


async def execute_memory_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "memory_recall":
            return await _recall(tool_input)
        elif tool_name == "memory_save":
            return await _save(tool_input)
        elif tool_name == "memory_list_topics":
            return await _list_topics(tool_input)
        elif tool_name == "memory_delete":
            return await _delete(tool_input)
        else:
            return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("Memory tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


async def _recall(tool_input: dict) -> str:
    client_key = tool_input.get("client_key", "")
    topic = tool_input.get("topic")
    query = tool_input.get("query")
    limit = int(tool_input.get("limit", 10))

    rows = await recall_memories(
        client_key=client_key,
        topic=topic,
        query=query,
        limit=limit,
    )

    if not rows:
        scope = f"cliente '{client_key}'" if client_key else "equipe (global)"
        topic_str = f" tópico '{topic}'" if topic else ""
        return f"Nenhuma memória encontrada para {scope}{topic_str}."

    lines = []
    for r in rows:
        date = r.get("created_at", "")[:10]
        lines.append(
            f"[ID:{r['id']} | {date} | {r['topic']} | fonte:{r['source']}]\n{r['content']}"
        )

    scope = f"*{client_key}*" if client_key else "*equipe (global)*"
    return f"Memórias de {scope} ({len(rows)} encontradas):\n\n" + "\n\n---\n".join(lines)


async def _save(tool_input: dict) -> str:
    client_key = tool_input.get("client_key", "")
    topic = tool_input.get("topic", "geral")
    content = tool_input.get("content", "")
    source = tool_input.get("source", "agent")

    if not content.strip():
        return "Erro: `content` não pode ser vazio."

    memory_id = await save_memory(
        client_key=client_key,
        topic=topic,
        content=content,
        source=source,
    )

    scope = f"*{client_key}*" if client_key else "*equipe (global)*"
    return (
        f"Memória salva (ID: `{memory_id}`):\n"
        f"• Cliente: {scope} | Tópico: `{topic}`\n"
        f"• Conteúdo: {content[:200]}"
    )


async def _list_topics(tool_input: dict) -> str:
    client_key = tool_input.get("client_key", "")
    topics = await list_memory_topics(client_key=client_key)

    if not topics:
        scope = f"'{client_key}'" if client_key else "equipe (global)"
        return f"Nenhum tópico de memória encontrado para {scope}."

    scope = f"*{client_key}*" if client_key else "*equipe (global)*"
    return f"Tópicos de memória de {scope}:\n" + "\n".join(f"• `{t}`" for t in topics)


async def _delete(tool_input: dict) -> str:
    memory_id = int(tool_input["memory_id"])
    removed = await delete_memory(memory_id)
    if removed:
        return f"Memória ID `{memory_id}` removida."
    return f"Memória ID `{memory_id}` não encontrada."
