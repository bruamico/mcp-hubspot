-- =============================================================================
-- Tropical Bot — Supabase Schema Migration v2
-- Run this in: Supabase Dashboard → SQL Editor → New query
--
-- Adds: user_permissions, dynamic_configs
-- =============================================================================

-- =============================================================================
-- 1. User permissions (RBAC)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.user_permissions (
    slack_user_id TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT 'user',
    granted_by    TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.user_permissions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    BEGIN
        EXECUTE 'CREATE POLICY "service_role_all" ON public.user_permissions'
            ' FOR ALL TO service_role USING (true) WITH CHECK (true)';
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END $$;

-- =============================================================================
-- 2. Dynamic configurations (bot-managed key/value store)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.dynamic_configs (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.dynamic_configs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    BEGIN
        EXECUTE 'CREATE POLICY "service_role_all" ON public.dynamic_configs'
            ' FOR ALL TO service_role USING (true) WITH CHECK (true)';
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END $$;
