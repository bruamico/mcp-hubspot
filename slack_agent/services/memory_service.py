"""
Intelligent persistent memory — three layers:

1. Semantic recall    — vector similarity via pgvector (OpenAI embeddings)
2. Proactive injection — relevant memories injected into context before each agent call
3. Auto-extraction    — Claude Haiku extracts and saves new facts after each conversation
"""
import asyncio
import json
import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"  # 1536 dims — matches Supabase schema

_EXTRACT_SYSTEM = """Você é um extrator de memórias para um assistente de CRM.
Analise a conversa abaixo e extraia APENAS fatos concretos e novos que valha salvar.

Retorne SOMENTE este JSON:
{
  "memories": [
    {
      "client_key": "chave_do_cliente_ou_vazio_para_global",
      "topic": "decisões|preferências|acionáveis|contexto|alertas|reuniões|leads",
      "content": "fato conciso com data e autor. Ex: Bruno (23/04): decidiu pausar onboarding Galena até junho por budget freeze."
    }
  ]
}

EXTRAIA quando detectar:
• Decisões tomadas: "decidimos X", "ficou definido Y"
• Preferências do cliente: "prefere comunicação direta", "não gosta de X"
• Mudança de contexto: "ponto de contato mudou para Maria", "projeto pausado"
• Alertas recorrentes: "cliente sempre demora responder às sextas"
• Resultado prático de reunião: participantes, decisão, próximo passo

NÃO EXTRAIA:
• Perguntas sem resposta definida
• Informações genéricas sem cliente específico
• Saudações, confirmações triviais ("ok, entendido")
• Nada relevante → {"memories": []}"""


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

async def generate_embedding(text: str) -> list[float] | None:
    """Generate a 1536-dim embedding via OpenAI API. Returns None if unavailable."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=api_key)
        resp = await client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=text[:8000],
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.warning("Embedding generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Layer 1 — Semantic recall
# ---------------------------------------------------------------------------

async def recall_semantic(
    query: str,
    client_key: str = "",
    limit: int = 8,
    min_similarity: float = 0.5,
) -> list[dict]:
    """
    Return memories most semantically similar to query.
    Falls back to keyword recall when OpenAI or Supabase is unavailable.
    """
    embedding = await generate_embedding(query)

    if embedding and os.getenv("SUPABASE_URL"):
        try:
            from ..models.supabase_db import _sb
            sb = await _sb()
            result = await sb.rpc("match_memories", {
                "query_embedding": embedding,
                "client_filter":   client_key.lower().strip(),
                "match_count":     limit,
                "min_similarity":  min_similarity,
            }).execute()
            if result.data:
                return result.data
        except Exception as exc:
            logger.warning("Semantic recall failed, falling back to keyword: %s", exc)

    # Keyword fallback
    from ..models.database import recall_memories
    return await recall_memories(client_key=client_key, query=query or None, limit=limit)


# ---------------------------------------------------------------------------
# Layer 2 — Proactive context injection
# ---------------------------------------------------------------------------

async def build_memory_context(user_message: str, known_clients: list[str]) -> str:
    """
    Build a compact memory block to prepend to the system prompt.

    Detects client names mentioned in the message with word-boundary matching,
    retrieves their most relevant memories (keyword-based for speed), and formats
    them as a ready-to-read context block.
    """
    from ..models.database import recall_memories

    msg_lower = user_message.lower()

    # Detect mentioned clients with word-boundary matching (avoids "ativa" → "ativação")
    mentioned = [
        k for k in known_clients
        if re.search(r'\b' + re.escape(k.lower()) + r'\b', msg_lower)
    ]

    # Always include global memories (client_key="")
    clients_to_query: list[str] = list(dict.fromkeys(mentioned + [""]))[:5]

    blocks = []
    for ck in clients_to_query:
        rows = await recall_memories(client_key=ck, limit=8)
        if not rows:
            continue
        label = ck.upper() if ck else "EQUIPE (global)"
        lines = [f"📌 *Memórias — {label}:*"]
        for r in rows:
            date = str(r.get("created_at", ""))[:10]
            lines.append(f"  • [{r['topic']}] {r['content']}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else ""


# ---------------------------------------------------------------------------
# Layer 3 — Auto-extraction (background task)
# ---------------------------------------------------------------------------

async def auto_extract_memories(
    user_message: str,
    assistant_response: str,
) -> int:
    """
    Analyze the latest exchange and auto-save any new facts to memory.
    Should be launched as an asyncio background task (fire-and-forget).
    Returns count of memories saved.
    """
    try:
        conversation = f"USUÁRIO: {user_message}\n\nASSISTENTE: {assistant_response[:2000]}"

        ac = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = await ac.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=800,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": conversation}],
        )

        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        memories = data.get("memories", [])

        saved = 0
        for m in memories:
            content = (m.get("content") or "").strip()
            if len(content) < 15:
                continue
            ck = (m.get("client_key") or "").lower().strip()
            topic = (m.get("topic") or "contexto").lower().strip()

            # Save and generate embedding in parallel
            embedding = await generate_embedding(content)
            await _save_with_embedding(ck, topic, content, "auto_extract", embedding)
            saved += 1

        if saved:
            logger.info("Auto-extracted %d memories from conversation", saved)
        return saved

    except json.JSONDecodeError:
        return 0
    except Exception as exc:
        logger.warning("auto_extract_memories failed: %s", exc)
        return 0


async def _save_with_embedding(
    client_key: str,
    topic: str,
    content: str,
    source: str,
    embedding: list[float] | None,
) -> int:
    """Save a memory and store its embedding vector (Supabase) or bytes blob (SQLite)."""
    if embedding and os.getenv("SUPABASE_URL"):
        try:
            from ..models.supabase_db import _sb
            sb = await _sb()
            result = await sb.table("memories").insert({
                "client_key": client_key,
                "topic":      topic,
                "content":    content,
                "source":     source,
                "embedding":  embedding,   # pgvector accepts list[float] via PostgREST
            }).execute()
            return result.data[0]["id"] if result.data else 0
        except Exception as exc:
            logger.warning("Supabase memory insert with embedding failed: %s", exc)

    # Fallback: keyword-only save (embedding stored as bytes in SQLite)
    from ..models.database import save_memory
    blob = None
    if embedding:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
    return await save_memory(client_key, topic, content, source, blob)
