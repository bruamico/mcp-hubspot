"""
Persistent memory tools for the Claude agent.

Lightweight mempalace-style memory stored in SQLite.
Organized by client_key (wing) + topic (room).

Wings  → client_key: 'galena', 'sympla', '' (global/equipe)
Rooms  → topic:      'decisões', 'preferências', 'acionáveis', 'contexto', 'reuniões'
"""
import logging

import numpy as np

from ..models.database import (
    save_memory, recall_memories, fetch_memories_with_embeddings,
    list_memory_topics, delete_memory,
)
from ..services.embeddings import embed, is_available

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

    if query and is_available():
        rows = await _semantic_recall(client_key, topic, query, limit)
        search_mode = "semântica"
    else:
        rows = await recall_memories(
            client_key=client_key,
            topic=topic,
            query=query,
            limit=limit,
        )
        search_mode = "palavra-chave" if query else "recente"

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
    return (
        f"Memórias de {scope} ({len(rows)} encontradas, busca {search_mode}):\n\n"
        + "\n\n---\n".join(lines)
    )


async def _semantic_recall(
    client_key: str,
    topic: str | None,
    query: str,
    limit: int,
) -> list[dict]:
    """
    Semantic recall: rank all stored memories by cosine similarity to the query.
    Falls back gracefully: memories without embeddings are ranked last (score 0)
    but included if they also match the query via LIKE.
    """
    query_emb = embed(query)
    if query_emb is None:
        # Embedding failed at runtime — fall back to LIKE
        return await recall_memories(client_key=client_key, topic=topic, query=query, limit=limit)

    # Fetch all candidates (up to 500) with their embeddings
    candidates = await fetch_memories_with_embeddings(
        client_key=client_key, topic=topic, limit=500
    )
    if not candidates:
        return []

    # For memories without embeddings: include if LIKE match, score 0.5
    # For memories with embeddings: score = cosine similarity
    query_lower = query.lower()
    q_vec = np.frombuffer(query_emb, dtype=np.float32)
    scored: list[tuple[float, dict]] = []
    for row in candidates:
        emb = row.get("embedding")
        if emb:
            score = float(np.dot(q_vec, np.frombuffer(emb, dtype=np.float32)))
        elif query_lower in (row.get("content") or "").lower():
            score = 0.5  # keyword match in legacy memory without embedding
        else:
            score = 0.0
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    # Filter out zero-score memories with no embedding and no keyword match
    results = [r for score, r in scored if score > 0.0]
    return results[:limit]


async def _save(tool_input: dict) -> str:
    client_key = tool_input.get("client_key", "")
    topic = tool_input.get("topic", "geral")
    content = tool_input.get("content", "")
    source = tool_input.get("source", "agent")

    if not content.strip():
        return "Erro: `content` não pode ser vazio."

    # Generate embedding for semantic recall — transparent, no error if unavailable
    embedding = embed(content)

    memory_id = await save_memory(
        client_key=client_key,
        topic=topic,
        content=content,
        source=source,
        embedding=embedding,
    )

    scope = f"*{client_key}*" if client_key else "*equipe (global)*"
    sem_tag = " 🧠" if embedding else ""
    return (
        f"Memória salva{sem_tag} (ID: `{memory_id}`):\n"
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
