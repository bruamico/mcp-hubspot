-- =============================================================================
-- Tropical Bot — Supabase Schema Migration v3
-- Run this in: Supabase Dashboard → SQL Editor → New query
--
-- Adds: match_memories() RPC function for semantic vector search
-- Requires: pgvector extension (already enabled in v1) and populated embeddings
-- =============================================================================

-- ---------------------------------------------------------------------------
-- match_memories: cosine similarity search over the memories table.
--
-- Returns memories ordered by similarity to the query embedding.
-- Includes both client-specific memories AND global memories (client_key='').
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.match_memories(
    query_embedding vector(1536),
    client_filter   TEXT    DEFAULT '',
    match_count     INT     DEFAULT 10,
    min_similarity  FLOAT   DEFAULT 0.5
)
RETURNS TABLE (
    id          BIGINT,
    client_key  TEXT,
    topic       TEXT,
    content     TEXT,
    source      TEXT,
    created_at  TIMESTAMPTZ,
    similarity  FLOAT
)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT
        m.id,
        m.client_key,
        m.topic,
        m.content,
        m.source,
        m.created_at,
        (1 - (m.embedding <=> query_embedding))::FLOAT AS similarity
    FROM public.memories m
    WHERE
        m.embedding IS NOT NULL
        AND (
            client_filter = ''
            OR m.client_key = client_filter
            OR m.client_key = ''        -- always include global memories
        )
        AND (1 - (m.embedding <=> query_embedding)) >= min_similarity
    ORDER BY m.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;
