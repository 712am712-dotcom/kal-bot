# Kal — Kalshi Crypto AI Trading Bot

An autonomous AI trading engine for [Kalshi](https://kalshi.com) crypto price prediction markets. Kal fetches live BTC/ETH/SOL spot prices from CoinGecko, feeds them into Claude for short-term price analysis, identifies edges vs the crowd's implied probability, and places trades when confidence and edge thresholds are met. All decisions are logged with full reasoning.

Discord notifications for every decision, trade, and daily P&L summary.

---

## Strategy

Kal focuses exclusively on **BTC, ETH, and SOL price prediction markets** on Kalshi — markets like "Bitcoin above $95,500 at 3pm?" that resolve in 15 minutes, 1 hour, or end-of-day.

**Why crypto?**
- High-frequency (new markets every 15 min), so Kal always has fresh opportunities
- Live spot prices from CoinGecko give Claude a concrete anchor: is the strike above or below the current price?
- Short timeframes = mean-reversion dynamics that Claude can model accurately
- Kalshi crowd is often slow to update during fast-moving markets

**Edge thesis:** When BTC is 2% above a strike with only 8 minutes left on a 15-min market, the crowd might still price YES at 70%. Kal catches these mispricings.

---

## Architecture

```
kalshi-bot/
├── /dashboard          Next.js 14 dashboard (deploys to Vercel)
├── /bot                Kal trading engine — Python 3.11 (deploys to Railway)
└── /supabase           Database schema
```

## How Kal Works

### Research Mode (default — before funding)
1. Kal fetches live BTC/ETH/SOL prices from CoinGecko
2. Scans all open crypto price prediction markets on Kalshi
3. Claude estimates the true YES probability for each market, with full live price context (spot price, strike distance, 24h momentum, time to close)
4. Decisions + full reasoning logged to Supabase
5. Strategy report generated: top opportunities, recommended funding amount
6. Discord notification sent with full summary
7. You review, decide whether to fund Kal

### Live Trading Mode (after funding)
1. Kal runs every 30 minutes
2. Fetches live crypto prices + open crypto markets above liquidity threshold
3. Claude estimates true probability with live context — if edge ≥ 6% and confidence ≥ 62% → evaluate risk controls
4. If all checks pass → place limit order
5. Discord alert fired for every trade (includes coin, strike, live price, 24h momentum)
6. Full audit trail logged to Supabase

---

## Setup

### 1. Supabase
1. Create a new [Supabase](https://supabase.com) project
2. Run `supabase/schema.sql` in the SQL editor
3. Copy your project URL, anon key, and service role key

### 2. Dashboard (Vercel)
```bash
cd dashboard
cp .env.local.example .env.local
# Fill in Supabase credentials
npm install
npm run dev
```

Deploy: push to GitHub → import in [Vercel](https://vercel.com), add env vars.

### 3. Kal Bot (Railway)
```bash
cd bot
cp .env.example .env
# Fill in all credentials
pip install -r requirements.txt

# Research mode (default — safe, no orders placed)
python main.py --research

# Live mode (real/demo trading)
python main.py --live
```

Deploy: create a new Railway project → connect GitHub → set env vars → deploy worker.

### 4. Discord Notifications (optional)
1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**
2. Copy the webhook URL
3. Add to `bot/.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

Kal will send alerts for every decision, trade, error, and periodic P&L summary — including live coin price, strike price, and 24h momentum for every crypto market analyzed.

---

## Risk Controls

| Control | Default | Description |
|---|---|---|
| `demo_mode` | `true` | No real orders placed |
| `research_mode` | `true` | Scan + analyze only, no orders |
| `market_focus` | `crypto` | `crypto` = BTC/ETH/SOL only, `all` = all Kalshi markets |
| `max_bet_dollars` | `$25` | Max dollars per trade |
| `daily_loss_limit_dollars` | `$50` | Stop trading after this daily loss |
| `crypto_min_confidence` | `62%` | Min Claude confidence for crypto markets |
| `crypto_min_edge` | `6%` | Min edge for crypto markets (tighter timeframes justify lower bar) |
| `min_confidence` | `70%` | Min Claude confidence for general markets |
| `min_edge` | `10%` | Min edge for general markets |
| `min_liquidity_dollars` | `$500` | Skip low-volume markets in live mode |
| `max_open_positions` | `10` | Cap concurrent positions |
| `research_max_markets` | `200` | Markets analyzed per research run |

All controls are editable in the dashboard (`/settings`) without redeploying.

---

## Security

- All API keys in environment variables — never in code
- Supabase RLS enabled on all tables
- Service role key only used server-side (API routes + Kal bot)
- Demo mode is default — live trading requires explicit toggle
- Full audit log of every decision and order
- RSA private key (Kalshi auth) excluded from git via `.gitignore`
- CoinGecko free tier — no API key required or stored

---

## Stack

| Layer | Tech |
|---|---|
| Dashboard | Next.js 14, TypeScript, Tailwind CSS, Recharts |
| Bot | Python 3.11, httpx, Anthropic SDK, Supabase Python |
| Database | Supabase (PostgreSQL + RLS) |
| AI | Anthropic Claude (claude-opus-4-6) |
| Markets | Kalshi Trade API v2 (RSA-SHA256 auth) — BTC/ETH/SOL crypto markets |
| Live Prices | CoinGecko API (free, no key required) |
| Notifications | Discord Webhooks |
| Hosting | Vercel (dashboard) + Railway (Kal bot) |
