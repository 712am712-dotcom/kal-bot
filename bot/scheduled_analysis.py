"""
scheduled_analysis.py — Daily briefings and weekly analysis for Kal.

Scheduled posts:
  7am ET daily  → #morning-brief  (ONE Claude call)
  8am ET daily  → #intelligence      (economic calendar, zero Claude)
  8am ET Monday → #weekly-analysis   (ONE Claude call)

Alpha Vantage free tier: 25 calls/day.
  → Fetch SPY, GLD, USO, QQQ once per day at market open (9:30am ET).
  → Cached for the rest of the day.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

ALPHA_BASE = "https://www.alphavantage.co/query"
COIN_FULL  = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}

# Assets fetched from Alpha Vantage (25 call budget — fetch once daily)
MACRO_SYMBOLS = {
    "SPY":  "S&P 500 ETF",
    "QQQ":  "Nasdaq ETF",
    "GLD":  "Gold ETF",
    "USO":  "Oil ETF",
    "SLV":  "Silver ETF",
}

# Module-level caches
_macro_cache:      dict[str, dict] = {}
_macro_cache_date: str             = ""
_briefing_posted_date: str         = ""
_weekly_posted_week:   str         = ""  # ISO week e.g. "2026-W12"


# ── Alpha Vantage ─────────────────────────────────────────────────────────────

async def fetch_macro_quote(symbol: str) -> dict:
    """Fetch latest daily quote for a symbol from Alpha Vantage."""
    key = settings.alpha_vantage_key
    if not key:
        return {}
    params = {
        "function": "GLOBAL_QUOTE",
        "symbol":   symbol,
        "apikey":   key,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(ALPHA_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
        quote = data.get("Global Quote", {})
        if not quote:
            return {}
        return {
            "symbol":   symbol,
            "price":    float(quote.get("05. price", 0)),
            "change":   float(quote.get("09. change", 0)),
            "change_p": float(quote.get("10. change percent", "0%").replace("%", "")),
        }
    except Exception as exc:
        log.warning("[scheduled] alpha_vantage %s failed: %s", symbol, exc)
        return {}


async def get_macro_data() -> dict[str, dict]:
    """
    Return cached macro data, refreshing once per day.
    Fetches all MACRO_SYMBOLS sequentially (respects 5 calls/min free limit).
    """
    global _macro_cache, _macro_cache_date
    today = datetime.date.today().isoformat()
    if _macro_cache_date == today and _macro_cache:
        return _macro_cache

    if not settings.alpha_vantage_key:
        return {}

    import asyncio
    data: dict[str, dict] = {}
    for symbol in MACRO_SYMBOLS:
        q = await fetch_macro_quote(symbol)
        if q:
            data[symbol] = q
        await asyncio.sleep(12.5)  # 5 calls/min = 12s gap to be safe
    _macro_cache      = data
    _macro_cache_date = today
    log.info("[scheduled] macro data fetched: %d symbols", len(data))
    return data


# ── Claude helpers ────────────────────────────────────────────────────────────

async def _claude_write(
    system: str, user: str, max_tokens: int = 600, model_override: str | None = None
) -> str:
    """One Claude call for narrative writing. Raises on failure."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=model_override or settings.claude_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


# ── Daily briefing ────────────────────────────────────────────────────────────

BRIEFING_SYSTEM = (
    "You are Kal, a crypto and prediction market analyst. "
    "Write a concise morning briefing. Plain text, no markdown headers (the caller adds those). "
    "Be direct and specific. 3-5 sentences max per section."
)

BRIEFING_PROMPT = """\
Write Kal's morning market briefing for {date}.

Crypto overnight:
{crypto_block}

Macro (if available):
{macro_block}

Top Kalshi markets right now (by volume):
{kalshi_block}

Write two sections — do NOT use markdown headers:
1. "Overnight summary:" — 2-3 sentences covering the overnight moves. Note any standout moves.
2. "Today's focus:" — 1-2 sentences on the most important thing to watch today.
3. "Key markets:" — mention 1-2 specific Kalshi markets and whether they look interesting.

Keep it to 6-8 sentences total. Sound like a trader, not a textbook.
"""


async def build_daily_briefing(
    crypto_prices: dict,
    ta_summaries: dict,    # {coin: {rsi, macd_cross, ...}} from TechnicalAnalyzer
    kalshi_markets: list[dict],
    macro_data: dict,
    model_override: str | None = None,
) -> str:
    """
    Build the full daily briefing text (ONE Claude call).
    Returns formatted Discord message content.
    """
    date_str = datetime.date.today().strftime("%A, %B %-d")

    # Crypto block
    crypto_lines = []
    for coin in ("BTC", "ETH", "SOL"):
        data = crypto_prices.get(coin, {})
        price = data.get("price", 0)
        chg   = data.get("change_24h", 0)
        rsi   = (ta_summaries.get(coin, {}).get("1h", {}) or {}).get("rsi")
        rsi_str = f" | RSI: {rsi}" if rsi else ""
        trend_ind = (ta_summaries.get(coin, {}).get("1h", {}) or {})
        ema9  = trend_ind.get("ema9")
        pr    = trend_ind.get("price")
        trend = ""
        if ema9 and pr:
            trend = " | Trending UP" if pr > ema9 else " | Trending DOWN"
        crypto_lines.append(
            f"  {COIN_FULL.get(coin, coin)}: ${price:,.0f} ({chg:+.1f}%){rsi_str}{trend}"
        )
    crypto_block = "\n".join(crypto_lines) if crypto_lines else "  Data unavailable"

    # Macro block
    macro_lines = []
    for sym, info in MACRO_SYMBOLS.items():
        d = macro_data.get(sym)
        if d:
            macro_lines.append(f"  {info} ({sym}): ${d['price']:.2f} ({d['change_p']:+.1f}%)")
    macro_block = "\n".join(macro_lines) if macro_lines else "  Alpha Vantage key not configured"

    # Top Kalshi markets by volume
    sorted_markets = sorted(
        kalshi_markets,
        key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
        reverse=True,
    )[:5]
    kalshi_lines = []
    for m in sorted_markets:
        title   = (m.get("title") or m.get("subtitle") or "?")[:60]
        vol_fp  = m.get("volume_fp")
        volume  = float(vol_fp) / 100.0 if vol_fp is not None else 0.0
        yes_ask = m.get("yes_ask_dollars")
        pct     = f"{float(yes_ask):.0%}" if yes_ask else "?"
        kalshi_lines.append(f"  {title} — {pct} | Vol ${volume:,.0f}")
    kalshi_block = "\n".join(kalshi_lines) if kalshi_lines else "  No markets loaded"

    prompt = BRIEFING_PROMPT.format(
        date=date_str,
        crypto_block=crypto_block,
        macro_block=macro_block,
        kalshi_block=kalshi_block,
    )

    narrative = await _claude_write(BRIEFING_SYSTEM, prompt, max_tokens=500, model_override=model_override)

    # Assemble final Discord message
    lines = [
        f"**Morning Briefing — {date_str}**",
        "",
        "**Overnight prices:**",
    ]
    for coin in ("BTC", "ETH", "SOL"):
        data = crypto_prices.get(coin, {})
        price = data.get("price", 0)
        chg   = data.get("change_24h", 0)
        rsi   = (ta_summaries.get(coin, {}).get("1h", {}) or {}).get("rsi")
        rsi_str = f" | RSI {rsi}" if rsi else ""
        lines.append(f"• {COIN_FULL.get(coin, coin)}: **${price:,.0f}** ({chg:+.1f}%){rsi_str}")

    if macro_data:
        lines.append("")
        for sym, info in MACRO_SYMBOLS.items():
            d = macro_data.get(sym)
            if d:
                lines.append(f"• {info}: **${d['price']:.2f}** ({d['change_p']:+.1f}%)")

    lines += ["", narrative]
    return "\n".join(lines)


# ── Weekly analysis ───────────────────────────────────────────────────────────

WEEKLY_SYSTEM = (
    "You are Kal, a crypto prediction market analyst. "
    "Write a weekly analysis for traders. Be specific, data-driven, and actionable. "
    "No fluff. Sound like a seasoned trader reviewing the tape."
)

WEEKLY_PROMPT = """\
Write Kal's weekly market analysis for the week of {week}.

Kal's paper trading last week:
  Trades: {total_trades} | Wins: {wins} | Losses: {losses} | P&L: ${pnl:+.2f}
  Best: {best_trade}
  Worst: {worst_trade}

Current crypto prices & technicals:
{crypto_block}

Top Kalshi opportunities Kal is watching:
{setups_block}

Write these sections (plain text, no markdown headers):
1. "Last week:" — 2 sentences on Kal's trading performance, honest assessment.
2. "Market review:" — 2-3 sentences on crypto moves this week, what mattered.
3. "This week's focus:" — 2 sentences on the single most important thing to watch.
4. "Top setups:" — mention 2-3 specific Kalshi markets worth watching and briefly why.

Keep to 8-10 sentences total.
"""


async def build_weekly_analysis(
    perf_stats: dict,
    crypto_prices: dict,
    ta_summaries: dict,
    kalshi_markets: list[dict],
    model_override: str | None = None,
) -> str:
    """Build weekly analysis text (ONE Claude call). Returns Discord message."""
    week_str = datetime.date.today().strftime("Week of %B %-d, %Y")

    crypto_lines = []
    for coin in ("BTC", "ETH", "SOL"):
        data = crypto_prices.get(coin, {})
        price = data.get("price", 0)
        chg   = data.get("change_24h", 0)
        ind1h = (ta_summaries.get(coin, {}).get("1h", {}) or {})
        rsi   = ind1h.get("rsi")
        macd  = ind1h.get("macd_cross", "")
        crypto_lines.append(
            f"  {COIN_FULL.get(coin, coin)}: ${price:,.0f} ({chg:+.1f}%)"
            + (f" | RSI {rsi}" if rsi else "")
            + (f" | MACD {macd}" if macd else "")
        )
    crypto_block = "\n".join(crypto_lines)

    # Top 5 Kalshi markets by volume
    sorted_m = sorted(
        kalshi_markets,
        key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
        reverse=True,
    )[:5]
    setups_block = "\n".join(
        f"  {(m.get('title') or '?')[:60]} — {float(m.get('yes_ask_dollars', 0.5) or 0.5):.0%}"
        for m in sorted_m
    )

    best  = perf_stats.get("best_trade",  (0.0, "none", ""))
    worst = perf_stats.get("worst_trade", (0.0, "none", ""))

    prompt = WEEKLY_PROMPT.format(
        week=week_str,
        total_trades=perf_stats.get("total_trades", 0),
        wins=perf_stats.get("wins", 0),
        losses=perf_stats.get("losses", 0),
        pnl=perf_stats.get("total_pnl", 0.0),
        best_trade=f"{best[1][:40]} (${best[0]:+.2f})" if best[0] else "none yet",
        worst_trade=f"{worst[1][:40]} (${worst[0]:+.2f})" if worst[0] else "none yet",
        crypto_block=crypto_block,
        setups_block=setups_block or "  No active markets",
    )

    narrative = await _claude_write(WEEKLY_SYSTEM, prompt, max_tokens=600, model_override=model_override)

    header = f"**Weekly Breakdown — {week_str}**"
    perf_line = (
        f"Last week: {perf_stats.get('wins',0)}W / {perf_stats.get('losses',0)}L | "
        f"P&L: ${perf_stats.get('total_pnl', 0):+.2f}"
    )
    return f"{header}\n{perf_line}\n\n{narrative}"


# ── Scheduler state ───────────────────────────────────────────────────────────

def should_post_daily_briefing() -> bool:
    """True once per day when UTC hour is 12 (≈ 7am–8am ET)."""
    global _briefing_posted_date
    today = datetime.date.today().isoformat()
    now_h = datetime.datetime.utcnow().hour
    if today != _briefing_posted_date and 12 <= now_h < 14:
        return True
    return False


def mark_briefing_posted() -> None:
    global _briefing_posted_date
    _briefing_posted_date = datetime.date.today().isoformat()


def should_post_weekly() -> bool:
    """True once per week on Monday when UTC hour is 13 (≈ 8am–9am ET)."""
    global _weekly_posted_week
    now   = datetime.datetime.utcnow()
    week  = now.strftime("%Y-W%W")
    if now.weekday() == 0 and week != _weekly_posted_week and 12 <= now.hour < 15:
        return True
    return False


def mark_weekly_posted() -> None:
    global _weekly_posted_week
    _weekly_posted_week = datetime.datetime.utcnow().strftime("%Y-W%W")
