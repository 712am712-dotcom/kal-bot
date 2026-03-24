"""
config.py — Kal configuration. All values loaded from environment variables.
Copy .env.example to .env and fill in real values before running.
"""
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # ── Kalshi API ──────────────────────────────────────────────────────────
    kalshi_api_key: str = Field(..., description="Kalshi API key ID (UUID from settings)")
    kalshi_private_key_path: str = Field(default="./kalshi_private_key.pem", description="Path to RSA private key .pem file")
    kalshi_demo: bool = Field(default=True, description="Use Kalshi demo env")

    # ── Anthropic ───────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    claude_model: str = Field(default="claude-opus-4-6", description="Model to use")
    claude_max_tokens: int = Field(default=1024)

    # ── Supabase ────────────────────────────────────────────────────────────
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_service_role_key: str = Field(..., description="Supabase service role key")

    # ── Risk Controls ───────────────────────────────────────────────────────
    max_bet_dollars: float = Field(default=25.0, description="Max dollars per trade")
    daily_loss_limit_dollars: float = Field(default=50.0, description="Stop trading after this daily loss")
    min_confidence: float = Field(default=0.70, description="Min Claude confidence (0.0–1.0)")
    min_edge: float = Field(default=0.10, description="Min probability edge vs Kalshi price")
    min_liquidity_dollars: float = Field(default=500.0, description="Min market volume to consider")
    max_open_positions: int = Field(default=10, description="Max concurrent open positions")

    # ── Mode Toggles ─────────────────────────────────────────────────────────
    demo_mode: bool = Field(default=True, description="Demo mode — no real orders")
    research_mode: bool = Field(default=True, description="Research mode — scan + analyze only")
    paper_trading: bool = Field(default=False, description="Paper trading — scan+trade on demo API, resolve every 5 min")

    # ── Scheduler ───────────────────────────────────────────────────────────
    scan_interval_minutes: int = Field(default=30, description="Live mode scan interval")
    research_scan_delay_seconds: float = Field(default=1.5, description="Delay between markets in research mode")
    research_max_markets: int = Field(default=200, description="Max markets to analyze per research run (cost control)")

    # ── Market Focus ─────────────────────────────────────────────────────────
    market_focus: str = Field(default="crypto", description="'crypto' = BTC/ETH/SOL only | 'all' = all Kalshi markets")
    # Per-coin thresholds — crypto short-term markets have tighter edges than political
    crypto_min_edge: float = Field(default=0.06, description="Min edge for crypto price markets (vs 0.10 for political)")
    crypto_min_confidence: float = Field(default=0.62, description="Min confidence for crypto markets (short-term = inherently noisier)")
    # Paper trading uses looser thresholds — tracking accuracy matters more than guarding capital
    paper_min_edge: float = Field(default=0.05, description="Min edge in paper trading mode")
    paper_min_confidence: float = Field(default=0.35, description="Min confidence in paper trading mode")

    # ── Risk — live mode has a stricter volume floor ──────────────────────────
    live_min_liquidity_dollars: float = Field(default=200.0, description="Live mode min volume — skip markets below this")

    # ── Discord — 4 separate channels ────────────────────────────────────────
    discord_webhook_url: str = Field(default="", description="Fallback webhook (legacy — use channel-specific ones below)")
    discord_webhook_trades:   str = Field(default="", description="Trades channel: placed + resolved")
    discord_webhook_analysis: str = Field(default="", description="Analysis channel: AI decisions")
    discord_webhook_summary:  str = Field(default="", description="Summary channel: periodic + daily reports")
    discord_webhook_alerts:        str = Field(default="", description="Alerts channel: errors + critical issues")
    discord_webhook_intelligence:  str = Field(default="", description="Intelligence channel: market alerts, hourly snapshot")
    discord_bot_token:             str = Field(default="", description="Bot token — required for channel creation, pinning, profile updates")

    # ── External APIs ─────────────────────────────────────────────────────────
    finnhub_api_key:       str = Field(default="", description="Finnhub free API key — finnhub.io")
    alpha_vantage_key:     str = Field(default="", description="Alpha Vantage free API key — alphavantage.co")
    fred_api_key:          str = Field(default="", description="FRED API key — fred.stlouisfed.org/docs/api/api_key.html (free)")
    newsletter_email:      str = Field(default="", description="[Legacy] Single newsletter sender — use NEWSLETTER_EMAILS instead")
    newsletter_emails:     str = Field(default="", description="Comma-separated list of newsletter senders for morning brief")
    gmail_credentials_path: str = Field(default="./gmail_credentials.json", description="Path to Gmail OAuth2 credentials JSON")
    gmail_token_path:      str = Field(default="./gmail_token.json", description="Path to saved Gmail OAuth2 token")
    # Generic IMAP (primary -- works with Outlook, Gmail, Yahoo, any provider)
    kal_email_address:     str = Field(default="", description="Kal's email address for IMAP auth (Outlook, Gmail, etc.)")
    kal_email_password:    str = Field(default="", description="Password for IMAP access -- Outlook account password or Gmail App Password")
    # Legacy Gmail-specific fields (kept for backward compat -- kal_email_address takes priority)
    kal_gmail_address:     str = Field(default="", description="[Legacy] Kal's Gmail address for App Password IMAP")
    kal_gmail_app_password: str = Field(default="", description="[Legacy] Gmail App Password")

    # ── Volume Floor ──────────────────────────────────────────────────────────
    volume_floor: float = Field(default=150.0, description="Skip markets below this dollar volume before calling Claude")

    # ── Intelligence Scanner ──────────────────────────────────────────────────
    intelligence_scan_interval: int = Field(default=30, description="Intelligence scanner interval in minutes")

    # ── Cost Controls ─────────────────────────────────────────────────────────
    active_coins:                  str   = Field(default="BTC,ETH,SOL", description="Comma-separated coins to analyze. Use 'BTC' to cut calls 3x.")
    daily_cost_limit_dollars:      float = Field(default=1.50,  description="Switch to Haiku when daily API spend hits this")
    daily_cost_warning_dollars:    float = Field(default=1.00,  description="Send #alerts warning when daily spend hits this")
    claude_fallback_model:         str   = Field(default="claude-haiku-4-5-20251001", description="Cheaper model used after daily_cost_limit is hit")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton — import this everywhere
settings = Settings()

# Kalshi base URLs
# Live endpoint confirmed at https://trading-api.readme.io/reference/getting-started
KALSHI_BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if settings.kalshi_demo
    else "https://api.elections.kalshi.com/trade-api/v2"
)
