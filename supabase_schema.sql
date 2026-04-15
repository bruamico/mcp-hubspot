-- =============================================================================
-- Tropical Bot — Supabase Schema Migration
-- Run this in: Supabase Dashboard → SQL Editor → New query
--
-- Prerequisites: the public.meetings table must already exist (created by the
-- readai-webhook Edge Function setup). This script adds the remaining tables
-- and enriches the meetings table.
-- =============================================================================

-- 1. Enable pgvector for semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- 2. Enrich the existing meetings table
-- =============================================================================

ALTER TABLE public.meetings
    ADD COLUMN IF NOT EXISTS embedding          vector(1536),
    ADD COLUMN IF NOT EXISTS commitments_extracted_at TIMESTAMPTZ;

-- Vector similarity index (used for semantic search once embeddings are populated)
CREATE INDEX IF NOT EXISTS idx_meetings_embedding
    ON public.meetings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- =============================================================================
-- 3. Full-text search function for meetings
--    Called via sb.rpc("search_meetings_fn", {...})
-- =============================================================================

CREATE OR REPLACE FUNCTION public.search_meetings_fn(
    q        TEXT,
    since_dt TEXT    DEFAULT NULL,
    lim      INTEGER DEFAULT 20
)
RETURNS TABLE (
    meeting_id         TEXT,
    title              TEXT,
    meeting_date       TIMESTAMPTZ,
    duration_seconds   INTEGER,
    participants       JSONB,
    summary            TEXT,
    action_items       JSONB,
    key_questions      JSONB,
    topics             JSONB,
    recording_url      TEXT,
    created_at         TIMESTAMPTZ
)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT
        m.meeting_id,
        m.title,
        m.meeting_date,
        m.duration_seconds,
        m.participants,
        m.summary,
        m.action_items,
        m.key_questions,
        m.topics,
        m.recording_url,
        m.created_at
    FROM public.meetings m
    WHERE (
        m.title              ILIKE '%' || q || '%'
        OR m.summary         ILIKE '%' || q || '%'
        OR m.participants::TEXT ILIKE '%' || q || '%'
        OR m.action_items::TEXT ILIKE '%' || q || '%'
        OR m.topics::TEXT    ILIKE '%' || q || '%'
    )
    AND (since_dt IS NULL OR m.meeting_date >= since_dt::TIMESTAMPTZ)
    ORDER BY COALESCE(m.meeting_date, m.created_at) DESC NULLS LAST
    LIMIT lim;
END;
$$;

-- =============================================================================
-- 4. Memories (persistent agent memory, with optional vector embedding)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.memories (
    id         BIGSERIAL PRIMARY KEY,
    client_key TEXT    NOT NULL DEFAULT '',
    topic      TEXT    NOT NULL DEFAULT 'geral',
    content    TEXT    NOT NULL,
    source     TEXT             DEFAULT 'agent',
    embedding  vector(1536),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_client
    ON public.memories(client_key);
CREATE INDEX IF NOT EXISTS idx_memories_topic
    ON public.memories(client_key, topic);
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON public.memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- =============================================================================
-- 5. Commitments (client task / delivery tracker)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.commitments (
    id             BIGSERIAL PRIMARY KEY,
    client_key     TEXT NOT NULL,
    description    TEXT NOT NULL,
    requested_by   TEXT,
    assigned_to    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    priority       TEXT NOT NULL DEFAULT 'normal',
    due_date       DATE,
    source_channel TEXT,
    source_ts      TEXT,
    fulfilled_at   TIMESTAMPTZ,
    notes          TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commitments_client
    ON public.commitments(client_key);
CREATE INDEX IF NOT EXISTS idx_commitments_status
    ON public.commitments(status);
-- Unique dedup index for extractor (source_channel + source_ts)
CREATE UNIQUE INDEX IF NOT EXISTS idx_commitments_source
    ON public.commitments(source_channel, source_ts)
    WHERE source_channel IS NOT NULL
      AND source_ts IS NOT NULL
      AND source_channel <> ''
      AND source_ts <> '';

-- =============================================================================
-- 6. Workspaces (dynamic Slack workspace registry)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.workspaces (
    key         TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    description TEXT,
    active      BOOLEAN     DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 7. OAuth tokens (Granola, Google Calendar, etc.)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.oauth_tokens (
    service       TEXT PRIMARY KEY,
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    BIGINT,
    scope         TEXT,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 8. Scheduled reports
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.scheduled_reports (
    id               BIGSERIAL PRIMARY KEY,
    client           TEXT,
    interval_minutes INTEGER NOT NULL,
    hours_back       INTEGER     DEFAULT 24,
    channel          TEXT NOT NULL,
    active           BOOLEAN     DEFAULT TRUE,
    last_run_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 9. Channel mappings (client_key → internal Tropical Hub channel)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.channel_mappings (
    client_key   TEXT PRIMARY KEY,
    channel_name TEXT NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 10. Delta monitoring jobs + snapshots
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.monitoring_jobs (
    id               BIGSERIAL PRIMARY KEY,
    client           TEXT,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    channel          TEXT NOT NULL,
    min_priority     TEXT NOT NULL DEFAULT 'orange',
    active           BOOLEAN     DEFAULT TRUE,
    last_run_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.monitoring_snapshots (
    client_key  TEXT PRIMARY KEY,
    context     TEXT             NOT NULL,
    captured_at DOUBLE PRECISION NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 11. Extraction watermarks (Slack commitment extractor progress tracking)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.extraction_watermarks (
    channel_id TEXT NOT NULL,
    workspace  TEXT NOT NULL DEFAULT 'internal',
    last_ts    TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (channel_id, workspace)
);

-- =============================================================================
-- 12. Alerts sent (dedup log for proactive scheduler alerts)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.alerts_sent (
    id        BIGSERIAL PRIMARY KEY,
    alert_key TEXT UNIQUE,
    channel   TEXT,
    message   TEXT,
    sent_at   TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 13. Row-Level Security
--     service_role: full access to everything
--     anon: read-only on meetings (already configured by Edge Function setup)
-- =============================================================================

ALTER TABLE public.memories             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitments          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspaces           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_tokens         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_reports    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.channel_mappings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monitoring_jobs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monitoring_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.extraction_watermarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alerts_sent          ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'memories', 'commitments', 'workspaces', 'oauth_tokens',
        'scheduled_reports', 'channel_mappings', 'monitoring_jobs',
        'monitoring_snapshots', 'extraction_watermarks', 'alerts_sent'
    ] LOOP
        BEGIN
            EXECUTE format(
                'CREATE POLICY "service_role_all" ON public.%I'
                ' FOR ALL TO service_role USING (true) WITH CHECK (true)',
                t
            );
        EXCEPTION WHEN duplicate_object THEN
            NULL; -- already exists, skip
        END;
    END LOOP;
END $$;
