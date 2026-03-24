"""
market_snapshot.py — Market open and market close posts for Kal.

Two functions:
  build_market_open()   — 9:30am ET, pure data, zero Claude calls
  build_market_close()  — 4:00pm ET, ONE Claude call per day

Market open data sources:
  - Kraken (BTC, ETH, SOL)
  - FRED (bond yields, via fred_client)
  - Alpha Vantage (Gold, Oil, DXY if key configured)
  - Finnhub (overnight news headline)

Market close data sources:
  - Kraken (crypto day change)
  - FRED (yield moves)
  - Finnhub (today's headlines)
  - Supabase trades table (today's Kal trades)
  - kal_lessons.json (yesterday's lesson)
  - Claude (synthesis + lesson generation — 1 call)

Scheduling (main.py):
  9:30am ET = 13:30 UTC (14:30 UTC in winter / standard time)
  4:00pm ET = 20:00 UTC (21:00 UTC in winter)
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_LESSONS_PATH = Path(__file__).parent / "kal_lessons.json"
_OPEN_STATE_PATH  = Path(__file__).parent / "market_open_state.json"
_CLOSE_STATE_PATH = Path(__file__).parent / "market_close_state.json"

# ── State helpers ─────────────────────────────────────────────────────────────

def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("[snapshot] state write failed %s: %s", path.name, exc)


def market_open_already_posted() -> bool:
    state = _read_state(_OPEN_STATE_PATH)
    return state.get("posted_date") == datetime.date.today().isoformat()


def mark_open_posted() -> None:
    state = _read_state(_OPEN_STATE_PATH)
    state["posted_date"] = datetime.date.today().isoformat()
    _write_state(_OPEN_STATE_PATH, state)


def market_close_already_posted() -> bool:
    state = _read_state(_CLOSE_STATE_PATH)
    return state.get("posted_date") == datetime.date.today().isoformat()


def mark_close_posted() -> None:
    state = _read_state(_CLOSE_STATE_PATH)
    state["posted_date"] = datetime.date.today().isoformat()
    _write_state(_CLOSE_STATE_PATH, state)


# ── Lessons file ──────────────────────────────────────────────────────────────

def load_lessons() -> dict:
    try:
        return json.loads(_LESSONS_PATH.read_text())
    except Exception:
        return {"last_updated": None, "today": None, "history": [], "running_themes": []}


def save_lesson(lesson_text: str, date_str: str) -> None:
    """
    Save today's lesson. Keeps a rolling 7-day history and extracts running themes.
    """
    data = load_lessons()

    # Push old "today" into history
    if data.get("today") and data.get("last_updated") != date_str:
        entry = {"date": data["last_updated"], "lesson": data["today"]}
        history = data.get("history", [])
        history.insert(0, entry)
        data["history"] = history[:7]  # keep 7 days

    data["today"]        = lesson_text
    data["last_updated"] = date_str

    # Simple running-themes extraction: track if same keywords appear >2 times in history
    all_lessons = [data["today"]] + [h["lesson"] for h in data.get("history", [])]
    theme_kws = ["yield", "bond", "rate", "BTC", "ETH", "momentum", "reversal",
                 "inflation", "fed", "spread", "overconfident", "volume"]
    theme_counts: dict[str, int] = {}
    for lesson in all_lessons:
        for kw in theme_kws:
            if kw.lower() in lesson.lower():
                theme_counts[kw] = theme_counts.get(kw, 0) + 1
    data["running_themes"] = [kw for kw, cnt in theme_counts.items() if cnt >= 3]

    try:
        _LESSONS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("[snapshot] failed to save lesson: %s", exc)


def get_yesterday_lesson() -> str | None:
    """Return the most recent lesson from yesterday (or last available)."""
    data = load_lessons()
    today = datetime.date.today().isoformat()
    # "today" might still be from yesterday if close hasn't run yet
    if data.get("today") and data.get("last_updated") != today:
        return data["today"]
    history = data.get("history", [])
    if history:
        return history[0]["lesson"]
    return None


def get_running_themes() -> list[str]:
    return load_lessons().get("running_themes", [])


# ── Alpha Vantage helpers ─────────────────────────────────────────────────────

async def _fetch_av_quote(symbol: str, av_key: str) -> float | None:
    """Fetch a daily close price from Alpha Vantage."""
    if not av_key:
        return None
    url = "https://www.alphavantage.co/query"
    params = {
        "function":   "GLOBAL_QUOTE",
        "symbol":     symbol,
        "apikey":     av_key,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        quote = data.get("Global Quote", {})
        price = quote.get("05. price")
        return float(price) if price else None
    except Exception as exc:
        log.debug("[snapshot] AV %s failed: %s", symbol, exc)
        return None


# ── Finnhub overnight news ────────────────────────────────────────────────────

async def _fetch_overnight_headline(finnhub_key: str) -> str:
    """Pull the top market-moving headline from Finnhub since last close."""
    if not finnhub_key:
        return ""
    try:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        today     = datetime.date.today().strftime("%Y-%m-%d")
        url = "https://finnhub.io/api/v1/news"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params={"category": "general", "token": finnhub_key})
            r.raise_for_status()
            articles = r.json()
        # Filter to recent articles
        relevant = [
            a for a in articles
            if a.get("category", "").lower() in ("general", "forex", "crypto", "merger")
        ]
        if relevant:
            top = relevant[0]
            return top.get("headline", "")[:200]
    except Exception as exc:
        log.debug("[snapshot] finnhub overnight failed: %s", exc)
    return ""


async def _fetch_today_headlines(finnhub_key: str) -> list[str]:
    """Return today's top 5 market headlines from Finnhub."""
    if not finnhub_key:
        return []
    try:
        url = "https://finnhub.io/api/v1/news"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params={"category": "general", "token": finnhub_key})
            r.raise_for_status()
            articles = r.json()
        return [a.get("headline", "") for a in articles[:5] if a.get("headline")]
    except Exception as exc:
        log.debug("[snapshot] finnhub today failed: %s", exc)
    return []


# ── Kraken helpers ────────────────────────────────────────────────────────────

async def _fetch_kraken_prices() -> dict[str, dict]:
    """Return BTC, ETH, SOL prices and 24h change from Kraken."""
    url = "https://api.kraken.com/0/public/Ticker"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, params={"pair": "XBTUSD,ETHUSD,SOLUSD"})
            r.raise_for_status()
            data = r.json()
        result = data.get("result", {})

        def _parse(key: str) -> tuple[float, float]:
            row   = result.get(key, {})
            last  = float(row.get("c", [0])[0])
            open_ = float(row.get("o", last) or last)
            chg   = ((last - open_) / open_ * 100) if open_ else 0.0
            return last, chg

        btc, btc_chg = _parse("XXBTZUSD")
        eth, eth_chg = _parse("XETHZUSD")
        sol, sol_chg = _parse("SOLUSD")
        return {
            "BTC": {"price": btc, "change_24h": btc_chg},
            "ETH": {"price": eth, "change_24h": eth_chg},
            "SOL": {"price": sol, "change_24h": sol_chg},
        }
    except Exception as exc:
        log.warning("[snapshot] kraken failed: %s", exc)
        return {}


def _fmt_price(p: float | None, decimals: int = 0) -> str:
    if p is None:
        return "N/A"
    return f"${p:,.{decimals}f}"


def _fmt_chg(c: float | None) -> str:
    if c is None:
        return ""
    sign = "+" if c >= 0 else ""
    return f"({sign}{c:.1f}%)"


def _et_now() -> str:
    """Current time in ET as a string."""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
    return et.strftime("%B %-d, %Y")


# ── Supabase helpers ──────────────────────────────────────────────────────────

async def _fetch_todays_trades(supabase_url: str, supabase_key: str) -> list[dict]:
    """Fetch today's trades from Supabase."""
    if not supabase_url or not supabase_key:
        return []
    try:
        today = datetime.date.today().isoformat()
        url   = f"{supabase_url}/rest/v1/ai_decisions"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                url,
                headers={
                    "apikey":        supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type":  "application/json",
                },
                params={
                    "select":       "ticker,market_title,recommendation,edge,confidence,result",
                    "created_at":   f"gte.{today}",
                    "order":        "created_at.desc",
                    "limit":        "20",
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        log.debug("[snapshot] trades fetch failed: %s", exc)
    return []


# ── Market open ───────────────────────────────────────────────────────────────

async def build_market_open(
    fred_api_key: str,
    finnhub_api_key: str,
    av_key: str,
) -> str:
    """
    Build the market open post. Pure data — zero Claude calls.
    Returns formatted Discord message.
    """
    import asyncio
    from fred_client import FredClient

    fred = FredClient(fred_api_key)

    # Fetch everything in parallel
    crypto_task    = _fetch_kraken_prices()
    fred_task      = fred.get_all()
    gold_task      = _fetch_av_quote("GLD", av_key)   # GLD ETF ≈ gold
    oil_task       = _fetch_av_quote("USO", av_key)   # USO ≈ WTI oil proxy
    dxy_task       = _fetch_av_quote("UUP", av_key)   # UUP ≈ dollar index proxy
    overnight_task = _fetch_overnight_headline(finnhub_api_key)

    crypto, fred_data, gold_raw, oil_raw, dxy_raw, overnight = await asyncio.gather(
        crypto_task, fred_task, gold_task, oil_task, dxy_task, overnight_task
    )

    # Use FRED gold/oil if AV unavailable
    gold = gold_raw or fred_data.get("gold")
    oil  = oil_raw  or fred_data.get("oil_wti")

    date_str = _et_now()

    lines = [f"**Market Open -- {date_str}**\n"]

    # Crypto line
    btc = crypto.get("BTC", {})
    eth = crypto.get("ETH", {})
    sol = crypto.get("SOL", {})
    crypto_line = (
        f"Crypto: Bitcoin {_fmt_price(btc.get('price'))} {_fmt_chg(btc.get('change_24h'))} | "
        f"Ethereum {_fmt_price(eth.get('price'))} | "
        f"Solana {_fmt_price(sol.get('price'), decimals=2)}"
    )
    lines.append(crypto_line)

    # Macro line
    gold_str = _fmt_price(gold)
    oil_str  = _fmt_price(oil)
    dxy_str  = f"{dxy_raw:.1f}" if dxy_raw else "N/A"
    macro_line = f"Macro: Gold {gold_str} | Oil {oil_str} | Dollar index {dxy_str}"
    lines.append(macro_line)

    # Bonds line
    if fred_data:
        lines.append(fred.market_open_bond_line(fred_data))

    # Overnight headline
    if overnight:
        lines.append(f"\nOvernight: {overnight}")

    # Watch today
    watch = _derive_watch_today(fred_data)
    lines.append(f"Watch today: {watch}")

    return "\n".join(lines)


def _derive_watch_today(fred_data: dict) -> str:
    """Data-only 'watch today' line based on bond signals."""
    if not fred_data:
        return "Monitor opening price action across crypto and equities."
    inv  = fred_data.get("yield_curve_inverted", False)
    hy   = fred_data.get("hy_spread")
    y10  = fred_data.get("yield_10y")

    if inv:
        return "Yield curve remains inverted — any flight-to-quality move in bonds will pressure growth assets."
    if hy is not None and hy > 5.5:
        return "High yield spreads are elevated — watch for risk-off moves across equities and crypto if credit stress intensifies."
    if y10 is not None and y10 > 4.5:
        return f"10Y Treasury at {y10:.2f}% is at elevated levels — continued yield pressure creates headwind for tech and crypto."
    return "No extreme macro signals — watch opening momentum and any reaction to overnight news."


# ── Market close ──────────────────────────────────────────────────────────────

async def build_market_close(
    fred_api_key: str,
    finnhub_api_key: str,
    av_key: str,
    supabase_url: str,
    supabase_key: str,
    anthropic_api_key: str,
    claude_model: str,
    model_override: str | None = None,
) -> tuple[str, float]:
    """
    Build the market close reflection. ONE Claude call.
    Returns (message_text, cost_dollars).
    """
    import asyncio
    from fred_client import FredClient

    fred  = FredClient(fred_api_key)
    model = model_override or claude_model

    # Fetch all data in parallel
    crypto_task    = _fetch_kraken_prices()
    fred_task      = fred.get_all()
    gold_task      = _fetch_av_quote("GLD", av_key)
    oil_task       = _fetch_av_quote("USO", av_key)
    headlines_task = _fetch_today_headlines(finnhub_api_key)
    trades_task    = _fetch_todays_trades(supabase_url, supabase_key)

    crypto, fred_data, gold_raw, oil_raw, headlines, trades = await asyncio.gather(
        crypto_task, fred_task, gold_task, oil_task, headlines_task, trades_task
    )

    yesterday_lesson = get_yesterday_lesson()
    themes = get_running_themes()

    # Build the Claude prompt
    date_str = _et_now()
    gold = gold_raw or fred_data.get("gold")
    oil  = oil_raw  or fred_data.get("oil_wti")

    btc  = crypto.get("BTC", {})
    eth  = crypto.get("ETH", {})
    sol  = crypto.get("SOL", {})

    context_lines = [
        f"Today's date: {date_str}",
        "",
        "PRICE DATA:",
        f"Bitcoin: {_fmt_price(btc.get('price'))} {_fmt_chg(btc.get('change_24h'))}",
        f"Ethereum: {_fmt_price(eth.get('price'))} {_fmt_chg(eth.get('change_24h'))}",
        f"Solana: {_fmt_price(sol.get('price'), 2)} {_fmt_chg(sol.get('change_24h'))}",
        f"Gold: {_fmt_price(gold)}",
        f"Oil (WTI proxy): {_fmt_price(oil)}",
    ]

    if fred_data:
        context_lines.append("")
        context_lines.append(fred.build_rotational_context(fred_data))

    if headlines:
        context_lines.append("")
        context_lines.append("TODAY'S HEADLINES:")
        for i, h in enumerate(headlines[:5], 1):
            context_lines.append(f"{i}. {h}")

    if trades:
        context_lines.append("")
        context_lines.append("KALSHI TRADES TODAY:")
        for t in trades[:10]:
            rec  = t.get("recommendation", "SKIP")
            edge = t.get("edge", 0)
            conf = t.get("confidence", 0)
            res  = t.get("result", "PENDING")
            context_lines.append(
                f"- {t.get('ticker', '?')}: {rec} | edge {edge:+.1%} | "
                f"conf {conf:.0%} | {res}"
            )

    if yesterday_lesson:
        context_lines.append("")
        context_lines.append(f"YESTERDAY'S LESSON: {yesterday_lesson}")

    if themes:
        context_lines.append("")
        context_lines.append(f"RUNNING THEMES (keep improving): {', '.join(themes)}")

    context = "\n".join(context_lines)

    system = """\
You are Kal, an AI trader who reads bonds, tracks macro rotation, and reflects on every trading day.

Your market close reflection is your most important post. It's where you connect the dots between
bonds, macro, sectors, and today's specific price action. Be honest about your trades.

Bond market leads everything. Always explain what yields are saying before explaining equity or crypto moves.

Rules:
- Be specific: use the actual prices, yields, and percentages provided
- If yields moved, say by how much and what it means for risk assets
- Connect bond moves to equity/crypto moves — show the transmission mechanism
- Acknowledge your wrong calls. Kal gets better by being honest about misses.
- Today's lesson must be specific and reference actual prices and events from today
- Yesterday's lesson check: did Kal improve on the miss? Be honest.

Respond ONLY with the formatted reflection post. No preamble. Use the exact format provided.
"""

    user_prompt = f"""\
Here is today's market data. Write the full Market Close reflection in this exact format:

**Market Close -- {date_str}**

What moved today:
- [Asset/Sector] +/-X% -- [one line reason]
- [Asset/Sector] +/-X% -- [one line reason]
(list 3-5 notable movers)

Bond market today:
- 10Y yield moved [X]% -- [what it means]
- Yield curve: [tightening/widening/still inverted] -- [implication]
- Credit spreads: [tightening=risk on / widening=risk off]

Where the money went:
[1-2 sentences on sector rotation -- what did institutions buy and sell today and why. Connect bond moves to equity moves.]

What it means tomorrow:
- [Key thing to watch at open]
- [Risk or catalyst to monitor]

Trade opportunities Kal sees:
- Kalshi: [prediction markets that look mispriced given today's action -- be specific about which]
- Crypto: [any crypto setup building -- Kal can execute these autonomously]
- Stocks: [any stock or sector worth watching -- flag for owner in #ideas]
- Bonds/Rates: [any rate market opportunity -- flag for owner in #ideas]
- Commodities: [oil/gold if relevant -- flag for owner in #ideas]

Kal's calls today:
[List each trade from the KALSHI TRADES TODAY section with WIN/LOSS/PENDING and one honest line on what Kal got right or wrong. If no trades, say "No trades placed today."]

Today's lesson: [specific lesson from today -- reference actual prices, yields, events]
Yesterday's lesson applied: [Did today's trading show improvement on yesterday's miss? Be honest and specific.]

---
MARKET DATA:
{context}
"""

    import time
    t0 = time.time()
    cost = 0.0
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text  = response.content[0].text.strip()
        inp   = response.usage.input_tokens
        out   = response.usage.output_tokens
        cost  = inp * 0.000003 + out * 0.000015  # Sonnet pricing (conservative)
        latency = round((time.time() - t0) * 1000)
        log.info("[snapshot] market close claude done tokens=%d+%d cost=$%.4f ms=%d",
                 inp, out, cost, latency)

        # Extract and save today's lesson
        lesson = _extract_lesson(text)
        if lesson:
            save_lesson(lesson, datetime.date.today().isoformat())

        return text, cost

    except Exception as exc:
        log.error("[snapshot] market close claude failed: %s", exc)
        # Fallback: data-only close post
        fallback = _build_fallback_close(date_str, crypto, fred_data, gold, oil, trades)
        return fallback, 0.0


def _extract_lesson(text: str) -> str:
    """Extract 'Today's lesson:' line from the close reflection."""
    for line in text.split("\n"):
        if line.strip().lower().startswith("today's lesson:"):
            return line.split(":", 1)[1].strip()
    return ""


def _build_fallback_close(
    date_str: str,
    crypto: dict,
    fred_data: dict,
    gold: float | None,
    oil: float | None,
    trades: list[dict],
) -> str:
    """Data-only fallback when Claude is unavailable."""
    from fred_client import FredClient
    fred = FredClient("")

    lines = [
        f"**Market Close -- {date_str}**",
        "",
        "_Claude unavailable for reflection — data summary:_",
        "",
    ]

    btc = crypto.get("BTC", {})
    eth = crypto.get("ETH", {})
    sol = crypto.get("SOL", {})

    lines.append("**Price summary:**")
    lines.append(f"- Bitcoin: {_fmt_price(btc.get('price'))} {_fmt_chg(btc.get('change_24h'))}")
    lines.append(f"- Ethereum: {_fmt_price(eth.get('price'))} {_fmt_chg(eth.get('change_24h'))}")
    lines.append(f"- Solana: {_fmt_price(sol.get('price'), 2)} {_fmt_chg(sol.get('change_24h'))}")
    if gold: lines.append(f"- Gold: {_fmt_price(gold)}")
    if oil:  lines.append(f"- Oil: {_fmt_price(oil)}")

    if fred_data:
        lines.append("")
        lines.append(fred.format_bond_block(fred_data))

    return "\n".join(lines)
