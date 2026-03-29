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

-- ── RSS articles log ──────────────────────────────────────────────────────────
-- One row per article evaluated by Kal's RSS intelligence system.
-- priority:  'high' | 'context' (keyword tier that triggered evaluation)
-- posted:    true if Claude decided to post to #market-pulse
-- channel:   Discord channel slug where the article was posted (or null)

CREATE TABLE IF NOT EXISTS rss_articles (
    id              SERIAL PRIMARY KEY,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    feed_name       TEXT NOT NULL,      -- e.g. "Reuters Business"
    article_url     TEXT NOT NULL,
    title           TEXT,
    published_at    TIMESTAMPTZ,
    priority        TEXT,               -- 'high' | 'context'
    claude_summary  TEXT,               -- Claude's one-line summary (null if skipped)
    posted          BOOLEAN DEFAULT FALSE,
    channel         TEXT                -- 'market-pulse' | 'breaking-news' | null
);

-- Unique constraint prevents re-processing the same URL
CREATE UNIQUE INDEX IF NOT EXISTS rss_articles_url_idx
    ON rss_articles (article_url);

-- Time-range queries
CREATE INDEX IF NOT EXISTS rss_articles_fetched_idx
    ON rss_articles (fetched_at DESC);

-- RLS: service role only
ALTER TABLE rss_articles ENABLE ROW LEVEL SECURITY;

CREATE POLICY IF NOT EXISTS "service role full access"
    ON rss_articles
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
