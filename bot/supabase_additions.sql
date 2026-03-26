-- supabase_additions.sql
-- Run this against your Supabase project to add the bot_communications table.
--
-- Usage:
--   supabase db execute < bot/supabase_additions.sql
-- Or paste into the Supabase SQL editor at app.supabase.com -> SQL Editor.

-- ── Bot communications log ────────────────────────────────────────────────────
-- Every message Kal sends is logged here regardless of Discord/email status.
-- delivery_method: 'discord' | 'email' | 'log_only'
-- status:          'delivered' | 'fallback_used' | 'failed'

CREATE TABLE IF NOT EXISTS bot_communications (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    channel         TEXT,
    message_type    TEXT,
    content         TEXT,
    delivery_method TEXT,   -- 'discord', 'email', 'log_only'
    status          TEXT    -- 'delivered', 'fallback_used', 'failed'
);

-- Index for time-range queries (e.g. "last 24 hours of failed messages")
CREATE INDEX IF NOT EXISTS bot_communications_timestamp_idx
    ON bot_communications (timestamp DESC);

-- Index for filtering by delivery method or status
CREATE INDEX IF NOT EXISTS bot_communications_status_idx
    ON bot_communications (status, delivery_method);

-- RLS: service role can insert freely; anon cannot read
ALTER TABLE bot_communications ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "service role full access"
    ON bot_communications
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
