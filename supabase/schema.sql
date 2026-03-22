-- ============================================================
-- kalshi-bot Supabase Schema
-- Run this in the Supabase SQL editor after creating a project.
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── markets_log ──────────────────────────────────────────────────────────────
-- Every market scanned by the bot (research or live mode)
CREATE TABLE IF NOT EXISTS markets_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id       TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT,
    yes_price       NUMERIC(6,4),           -- 0.0000 to 1.0000
    no_price        NUMERIC(6,4),
    volume          NUMERIC(14,2),
    open_interest   NUMERIC(14,2),
    close_time      TIMESTAMPTZ,
    scanned_at      TIMESTAMPTZ DEFAULT NOW(),
    scan_mode       TEXT NOT NULL CHECK (scan_mode IN ('research', 'live'))
);

CREATE INDEX IF NOT EXISTS markets_log_ticker_idx   ON markets_log (ticker);
CREATE INDEX IF NOT EXISTS markets_log_scanned_idx  ON markets_log (scanned_at DESC);
CREATE INDEX IF NOT EXISTS markets_log_category_idx ON markets_log (category);

-- ── ai_decisions ─────────────────────────────────────────────────────────────
-- Every Claude API call and structured response
CREATE TABLE IF NOT EXISTS ai_decisions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    market_id               TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    market_title            TEXT NOT NULL,
    claude_yes_probability  NUMERIC(6,4),   -- Claude's estimate
    kalshi_yes_price        NUMERIC(6,4),   -- Kalshi's current price at time of call
    edge                    NUMERIC(7,4),   -- claude_yes_probability - kalshi_yes_price (can be negative)
    confidence              NUMERIC(6,4),
    reasoning               TEXT,
    recommendation          TEXT NOT NULL CHECK (recommendation IN ('BUY_YES', 'BUY_NO', 'SKIP')),
    decision_mode           TEXT NOT NULL CHECK (decision_mode IN ('research', 'live')),
    tokens_used             INTEGER,
    latency_ms              INTEGER,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ai_decisions_ticker_idx    ON ai_decisions (ticker);
CREATE INDEX IF NOT EXISTS ai_decisions_created_idx   ON ai_decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS ai_decisions_rec_idx       ON ai_decisions (recommendation);
CREATE INDEX IF NOT EXISTS ai_decisions_mode_idx      ON ai_decisions (decision_mode);

-- ── trades ───────────────────────────────────────────────────────────────────
-- Every trade placed (real or demo)
CREATE TABLE IF NOT EXISTS trades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ai_decision_id      UUID REFERENCES ai_decisions (id) ON DELETE SET NULL,
    market_id           TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    market_title        TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    contracts           INTEGER NOT NULL,
    price_cents         INTEGER NOT NULL CHECK (price_cents BETWEEN 1 AND 99),
    amount_dollars      NUMERIC(10,2) NOT NULL,
    order_id            TEXT,                   -- Kalshi order ID (null for failed orders)
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'filled', 'partial', 'cancelled', 'failed')),
    filled_contracts    INTEGER DEFAULT 0,
    filled_price_cents  INTEGER,
    pnl_dollars         NUMERIC(10,2),          -- set after market resolution
    resolved_at         TIMESTAMPTZ,
    resolution          TEXT CHECK (resolution IN ('yes', 'no', 'void')),
    demo_mode           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS trades_ticker_idx      ON trades (ticker);
CREATE INDEX IF NOT EXISTS trades_status_idx      ON trades (status);
CREATE INDEX IF NOT EXISTS trades_created_idx     ON trades (created_at DESC);
CREATE INDEX IF NOT EXISTS trades_order_id_idx    ON trades (order_id);
CREATE INDEX IF NOT EXISTS trades_demo_idx        ON trades (demo_mode);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── bot_config ────────────────────────────────────────────────────────────────
-- Risk settings, mode toggles — editable from the dashboard
CREATE TABLE IF NOT EXISTS bot_config (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key         TEXT UNIQUE NOT NULL,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Seed default config values
INSERT INTO bot_config (key, value, description) VALUES
    ('demo_mode',                   'true',  'Demo mode — no real orders placed'),
    ('research_mode',               'true',  'Research mode — scan and analyze only'),
    ('max_bet_dollars',             '25',    'Max dollars per trade'),
    ('daily_loss_limit_dollars',    '50',    'Stop trading after this daily loss'),
    ('min_confidence',              '0.70',  'Min Claude confidence threshold'),
    ('min_edge',                    '0.10',  'Min edge vs Kalshi price'),
    ('min_liquidity_dollars',       '500',   'Min market volume'),
    ('max_open_positions',          '10',    'Max concurrent open positions'),
    ('scan_interval_minutes',       '30',    'Live mode scan interval')
ON CONFLICT (key) DO NOTHING;

-- ── daily_summary ─────────────────────────────────────────────────────────────
-- End-of-day snapshot generated by the bot
CREATE TABLE IF NOT EXISTS daily_summary (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date                    DATE UNIQUE NOT NULL,
    markets_scanned         INTEGER DEFAULT 0,
    ai_calls_made           INTEGER DEFAULT 0,
    trades_placed           INTEGER DEFAULT 0,
    trades_filled           INTEGER DEFAULT 0,
    gross_pnl_dollars       NUMERIC(10,2) DEFAULT 0,
    net_pnl_dollars         NUMERIC(10,2) DEFAULT 0,
    total_wagered_dollars   NUMERIC(10,2) DEFAULT 0,
    win_rate                NUMERIC(6,4),
    best_category           TEXT,
    worst_category          TEXT,
    demo_mode               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS daily_summary_date_idx ON daily_summary (date DESC);

-- ── strategy_report ───────────────────────────────────────────────────────────
-- Research mode output: best categories, recommended funding
CREATE TABLE IF NOT EXISTS strategy_report (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_date             DATE NOT NULL,
    research_start          TIMESTAMPTZ,
    research_end            TIMESTAMPTZ,
    markets_analyzed        INTEGER DEFAULT 0,
    categories_analyzed     JSONB,      -- array of CategoryStat objects
    top_categories          JSONB,      -- top 3 recommended categories
    recommended_bet_size    NUMERIC(10,2),
    recommended_funding     NUMERIC(10,2),
    overall_edge            NUMERIC(6,4),
    summary                 TEXT,
    full_report             JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS strategy_report_date_idx ON strategy_report (report_date DESC);

-- ── Row Level Security ────────────────────────────────────────────────────────
-- All tables are locked down by default.
-- The bot uses the service role key (bypasses RLS).
-- The dashboard uses the service role key server-side only.
-- Anon / authenticated users have read-only access.

ALTER TABLE markets_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_decisions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades           ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_config       ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_summary    ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_report  ENABLE ROW LEVEL SECURITY;

-- Read-only policies for authenticated users (dashboard browser client)
CREATE POLICY "authenticated read markets_log"
    ON markets_log FOR SELECT TO authenticated USING (true);

CREATE POLICY "authenticated read ai_decisions"
    ON ai_decisions FOR SELECT TO authenticated USING (true);

CREATE POLICY "authenticated read trades"
    ON trades FOR SELECT TO authenticated USING (true);

CREATE POLICY "authenticated read bot_config"
    ON bot_config FOR SELECT TO authenticated USING (true);

CREATE POLICY "authenticated read daily_summary"
    ON daily_summary FOR SELECT TO authenticated USING (true);

CREATE POLICY "authenticated read strategy_report"
    ON strategy_report FOR SELECT TO authenticated USING (true);

-- Service role has full access (used by bot and dashboard API routes)
-- No additional policies needed — service role bypasses RLS automatically.
