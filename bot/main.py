"""
main.py — Orchestrator for Kal, the Kalshi AI trading bot.

Modes:
  RESEARCH MODE (settings.research_mode = True):
    - Scans all open markets
    - Calls Claude for each market to estimate true probability
    - Logs decisions to DB (no real orders)
    - Generates a strategy_report with best categories + funding recommendation

  LIVE MODE (settings.research_mode = False):
    - Runs every 30 minutes (configurable via SCAN_INTERVAL_MINUTES)
    - Scans all open markets that pass liquidity filter
    - Calls Claude, checks risk controls, places orders when edge found
    - Syncs open order fills each cycle

Usage:
  python main.py [--research | --live | --paper]
  --paper: demo API, live scan loop, resolve every 5 min (no real money).
  Defaults to whatever settings.research_mode is set to.
"""
import asyncio
import datetime
import json
import sys
import time

import httpx
import structlog

from claude_client import ClaudeClient
from config import settings
from db_logger import DBLogger
from decision_engine import DecisionEngine
import discord_notifier as discord
from kalshi_client import KalshiClient
from market_intelligence import IntelligenceScanner
from news_intelligence import NewsIntelligence
from order_manager import OrderManager
from scheduled_analysis import (
    build_daily_briefing, build_weekly_analysis, get_macro_data,
    should_post_daily_briefing, mark_briefing_posted,
    should_post_weekly, mark_weekly_posted,
)
from email_reader import EmailReader as GmailReader, build_morning_brief, extract_todays_focus, evaluate_axios_alert
import email_reader as _er
from technical_analysis import TechnicalAnalyzer
from performance_tracker import PerformanceTracker
import journal as journal_mod
from economic_calendar import (
    post_weekly_calendar, get_daily_calendar_alert,
    weekly_already_posted, mark_weekly_posted,
    daily_alert_already_posted, mark_daily_alert_posted,
    _is_sunday_8am_et, _is_weekday_7am_et,
)
from market_snapshot import (
    build_market_open, build_market_close,
    market_open_already_posted, mark_open_posted,
    market_close_already_posted, mark_close_posted,
)
from ideas_channel import evaluate_for_ideas, already_posted_today as ideas_posted_today

log = structlog.get_logger(__name__)

# ── Per-window deduplication ──────────────────────────────────────────────────
# Tracks which tickers have been analyzed/traded in the current 15min window.
# Reset automatically when a new window's close_time is detected.
_current_window_close: str = ""       # close_time of the window we're currently in
_analyzed_this_window: set[str] = set()  # tickers already processed this window

# ── Kraken price cache (per-window) ──────────────────────────────────────────
_price_cache:        dict = {}   # last successful price fetch
_price_cache_window: str  = ""   # window close time when cache was populated

# ── Daily API cost tracking ───────────────────────────────────────────────────
_daily_cost_dollars:  float = 0.0
_daily_cost_calls:    int   = 0
_daily_fallback_calls: int  = 0
_daily_cost_date:     str   = ""    # YYYY-MM-DD, reset each midnight
_cost_warning_sent:   bool  = False
_use_fallback_model:  bool  = False

# ── Error guard state ─────────────────────────────────────────────────────────
# Prevents repeated error spam to #alerts.
_credit_error_active: bool = False   # credit alert already posted once
_credits_exhausted:   bool = False   # both primary+fallback failed → 30 min pause
_error_cooldown: dict[str, datetime.datetime] = {}  # key → last Discord post time

_CREDIT_ERR_TEXT   = "credit balance is too low"
_CREDIT_PAUSE_SECS = 1800  # 30 minutes between retry cycles when exhausted

# ── Error guard helpers ───────────────────────────────────────────────────────

def _is_credit_error(exc: Exception) -> bool:
    return _CREDIT_ERR_TEXT in str(exc).lower()


def _clean_error_msg(exc: Exception) -> str:
    """Strip tracebacks and API boilerplate. Return a single plain-English line."""
    import re
    msg = str(exc)
    # Drop everything from "Traceback" onward
    if "Traceback" in msg:
        msg = msg[:msg.index("Traceback")].strip()
    # Anthropic API errors embed the real message as 'message': '...'
    m = re.search(r"'message':\s*'([^']+)'", msg)
    if m:
        return m.group(1)[:200]
    return msg[:200]


async def _report_error(exc: Exception, context: str, critical: bool = False) -> None:
    """
    Smart error reporter — deduplicates, strips tracebacks, formats cleanly.

    Credit errors:  post ONCE until credits are restored; then silence.
    All others:     post at most once per 60 minutes per error signature.
    Never post raw tracebacks.
    """
    global _credit_error_active, _error_cooldown

    now = datetime.datetime.utcnow()

    # Credit error — post once, then go silent
    if _is_credit_error(exc):
        if not _credit_error_active:
            _credit_error_active = True
            await discord.notify_credits_exhausted()
        else:
            log.debug("credit_error_suppressed_already_alerted")
        return

    # All other errors — 60 min cooldown per error signature
    key = f"{type(exc).__name__}:{str(exc)[:60]}"
    last = _error_cooldown.get(key)
    if last and (now - last).total_seconds() < 3600:
        log.debug("error_suppressed_cooldown", key=key[:80])
        return
    _error_cooldown[key] = now

    clean = _clean_error_msg(exc)
    await discord.notify_error(context=context, error=clean, critical=critical)


async def _note_api_success() -> None:
    """Call after any successful Claude API call to detect credit restoration."""
    global _credit_error_active, _credits_exhausted
    if _credit_error_active or _credits_exhausted:
        _credit_error_active = False
        _credits_exhausted   = False
        await discord.notify_credits_restored()


def _seconds_until_next_scan() -> float:
    """
    Returns how many seconds to sleep before the next scan.
    If we know the current window's close_time, sleep until it closes + 20s buffer.
    Otherwise fall back to scan_interval_minutes.
    """
    global _current_window_close
    if _current_window_close:
        try:
            close_dt = datetime.datetime.fromisoformat(
                _current_window_close.replace("Z", "+00:00")
            )
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            secs_left = (close_dt - now_utc).total_seconds()
            # Sleep until window closes + 20s so markets have time to finalize
            return max(10.0, secs_left + 20.0)
        except Exception:
            pass
    return settings.scan_interval_minutes * 60


# ── Active coins helper ───────────────────────────────────────────────────────

def _get_active_coins() -> set[str]:
    """Return the set of coins Kal should analyze (from ACTIVE_COINS env var)."""
    raw = getattr(settings, "active_coins", "BTC,ETH,SOL")
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


# ── Daily cost tracker ────────────────────────────────────────────────────────

async def _record_api_cost(cost: float, is_fallback: bool, db: "DBLogger") -> None:
    """
    Accumulate today's API spend. On crossing $1.00 send a warning.
    On crossing $1.50 switch to Haiku for the rest of the day and send an alert.
    Persists daily total to bot_config so it survives restarts.
    """
    global _daily_cost_dollars, _daily_cost_calls, _daily_fallback_calls
    global _daily_cost_date, _cost_warning_sent, _use_fallback_model

    today = datetime.date.today().isoformat()
    if today != _daily_cost_date:
        # New day — reset all counters and re-read any persisted total from DB
        _daily_cost_date    = today
        _cost_warning_sent  = False
        _use_fallback_model = False
        try:
            stored = await db.get_config(f"daily_cost_{today}")
            _daily_cost_dollars = float(stored) if stored else 0.0
            calls_str = await db.get_config(f"daily_calls_{today}")
            _daily_cost_calls = int(calls_str) if calls_str else 0
        except Exception:
            _daily_cost_dollars = 0.0
            _daily_cost_calls   = 0
        _daily_fallback_calls = 0

    _daily_cost_dollars += cost
    _daily_cost_calls   += 1
    if is_fallback:
        _daily_fallback_calls += 1

    # Persist so a restart mid-day doesn't lose the count
    try:
        await db.set_config(f"daily_cost_{today}", str(round(_daily_cost_dollars, 6)))
        await db.set_config(f"daily_calls_{today}", str(_daily_cost_calls))
    except Exception:
        pass

    log.debug(
        "api_cost_recorded",
        call_cost=f"${cost:.5f}",
        daily_total=f"${_daily_cost_dollars:.4f}",
        calls=_daily_cost_calls,
        fallback=_use_fallback_model,
    )

    warning_limit = settings.daily_cost_warning_dollars
    hard_limit    = settings.daily_cost_limit_dollars

    if not _cost_warning_sent and _daily_cost_dollars >= warning_limit:
        _cost_warning_sent = True
        try:
            import journal as _j
            state       = _j._read_state()
            weekly_pace = _daily_cost_dollars * 7
            await discord.notify_cost_warning(
                spent=_daily_cost_dollars,
                warning_limit=warning_limit,
                hard_limit=hard_limit,
                weekly_pace=weekly_pace,
                trades_placed=state.get("today_trades", 0),
                wins=state.get("wins",   0),
                losses=state.get("losses", 0),
            )
        except Exception as exc:
            log.warning("cost_warning_notify_failed", error=str(exc))

    if not _use_fallback_model and _daily_cost_dollars >= hard_limit:
        _use_fallback_model = True
        fallback = settings.claude_fallback_model
        log.info("switching_to_fallback_model", model=fallback, daily_cost=f"${_daily_cost_dollars:.4f}")
        try:
            await discord.notify_cost_limit_hit(
                spent=_daily_cost_dollars,
                hard_limit=hard_limit,
                fallback_model=fallback,
            )
        except Exception as exc:
            log.warning("cost_limit_notify_failed", error=str(exc))


# ── Kraken live prices ────────────────────────────────────────────────────────

async def fetch_crypto_prices(window_close: str = "") -> dict:
    """
    Fetch live BTC, ETH, SOL spot prices + 24h change from Kraken.
    Free, no API key, no geo-restrictions. Caches result per 15-minute window.
    On any error, returns the last cached prices so trading is never blocked.
    """
    global _price_cache, _price_cache_window

    # Return cached result if we're still in the same window
    if window_close and window_close == _price_cache_window and _price_cache:
        log.debug("crypto_prices_from_cache", window=window_close)
        return _price_cache

    # Kraken pair names: XBT=BTC, ETH=ETH, SOL=SOL (all vs USD)
    url = "https://api.kraken.com/0/public/Ticker"
    params = {"pair": "XBTUSD,ETHUSD,SOLUSD"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        result = data.get("result", {})
        # Kraken keys: XXBTZUSD, XETHZUSD, SOLUSD
        # 'c' = [last_trade_price, lot_volume], 'o' = today's open price
        def _parse(key: str) -> tuple[float, float]:
            row   = result.get(key, {})
            last  = float(row.get("c", [0])[0])
            open_ = float(row.get("o", last) or last)
            chg   = ((last - open_) / open_ * 100) if open_ else 0.0
            return last, chg

        btc_price, btc_chg = _parse("XXBTZUSD")
        eth_price, eth_chg = _parse("XETHZUSD")
        sol_price, sol_chg = _parse("SOLUSD")

        prices = {
            "BTC": {"price": btc_price, "change_24h": btc_chg},
            "ETH": {"price": eth_price, "change_24h": eth_chg},
            "SOL": {"price": sol_price, "change_24h": sol_chg},
        }
        log.info(
            "crypto_prices_fetched",
            BTC=f"${prices['BTC']['price']:,.0f} ({prices['BTC']['change_24h']:+.2f}%)",
            ETH=f"${prices['ETH']['price']:,.0f} ({prices['ETH']['change_24h']:+.2f}%)",
            SOL=f"${prices['SOL']['price']:,.2f} ({prices['SOL']['change_24h']:+.2f}%)",
        )
        _price_cache        = prices
        _price_cache_window = window_close
        return prices
    except Exception as exc:
        if _price_cache:
            log.warning("kraken_prices_failed_using_cache", error=str(exc))
            return _price_cache
        log.warning("kraken_prices_failed_no_cache", error=str(exc))
        return {}

# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_market_fields(market: dict) -> dict:
    """
    Normalize a Kalshi market dict into the fields we need.

    Kalshi API v2 field names (confirmed from docs):
      Price:  yes_ask_dollars, yes_bid_dollars  (dollars, NOT cents)
      Volume: volume_fp  (fixed-point integer, divide by 100 for dollars)
              open_interest_fp
    """
    # Price: prefer ask, fall back to bid, then default 0.50
    yes_ask = market.get("yes_ask_dollars")
    yes_bid = market.get("yes_bid_dollars")
    if yes_ask is not None:
        yes_price = float(yes_ask)
    elif yes_bid is not None:
        yes_price = float(yes_bid)
    else:
        yes_price = 0.50

    # Clamp to valid probability range
    yes_price = max(0.01, min(0.99, yes_price))
    no_price  = round(1.0 - yes_price, 6)

    # Volume: volume_fp is a fixed-point integer (cents), divide by 100
    volume_fp = market.get("volume_fp")
    volume    = float(volume_fp) / 100.0 if volume_fp is not None else 0.0

    oi_fp          = market.get("open_interest_fp")
    open_interest  = float(oi_fp) / 100.0 if oi_fp is not None else 0.0

    return {
        "market_id":     market.get("id", ""),
        "ticker":        market.get("ticker", ""),
        "title":         market.get("title", market.get("subtitle", "Unknown")),
        "category":      market.get("category", ""),
        "yes_price":     yes_price,
        "no_price":      no_price,
        "volume":        volume,
        "open_interest": open_interest,
        "close_time":    market.get("close_time", ""),
    }


def compute_tradeable_score(
    edge: float,
    confidence: float,
    volume: float,
) -> tuple[float, float]:
    """
    Returns (tradeable_score, expected_profit_dollars).

    tradeable_score (0.0–1.0):
      abs(edge) * confidence * liquidity_factor
      liquidity_factor scales 0 → 1 as volume goes $0 → $500.
      Zero-volume markets score at 30% of their liquid equivalent —
      flagged as research-only, not live-tradeable.

    expected_profit_dollars:
      abs(edge) * max_bet_dollars — the EV of one max-size trade.
    """
    edge_abs         = abs(edge)
    liquidity_cap    = max(settings.min_liquidity_dollars, 1.0)
    liquidity_factor = min(volume / liquidity_cap, 1.0)
    # 30% base score even at zero volume so they still appear in research rankings
    tradeable_score  = edge_abs * confidence * (0.3 + 0.7 * liquidity_factor)
    expected_profit  = edge_abs * settings.max_bet_dollars
    return round(tradeable_score, 4), round(expected_profit, 2)


def compute_category_stats(decisions: list[dict]) -> list[dict]:
    """Aggregate decisions by category for strategy report."""
    from collections import defaultdict
    cats: dict[str, list] = defaultdict(list)
    for d in decisions:
        if d.get("recommendation") != "SKIP":
            cats[d.get("category", "unknown")].append(d)

    stats = []
    for cat, rows in cats.items():
        edges   = [abs(r["edge"]) for r in rows]
        profits = [r.get("expected_profit", 0) for r in rows]
        scores  = [r.get("tradeable_score", 0) for r in rows]
        liquid  = [r for r in rows if r.get("volume", 0) >= settings.min_liquidity_dollars]
        stats.append({
            "category":          cat,
            "sample_size":       len(rows),
            "liquid_count":      len(liquid),
            "zero_volume_count": sum(1 for r in rows if r.get("volume", 0) == 0),
            "edge_avg":          round(sum(edges)   / len(edges),   4) if edges   else 0,
            "profit_avg":        round(sum(profits) / len(profits), 2) if profits else 0,
            "score_avg":         round(sum(scores)  / len(scores),  4) if scores  else 0,
            "buy_yes_count":     sum(1 for r in rows if r["recommendation"] == "BUY_YES"),
            "buy_no_count":      sum(1 for r in rows if r["recommendation"] == "BUY_NO"),
        })

    # Sort by tradeable_score so liquid+high-edge categories float to top
    return sorted(stats, key=lambda x: x["score_avg"], reverse=True)

# ── Research Mode ─────────────────────────────────────────────────────────────

async def run_research_scan(
    kalshi:  KalshiClient,
    claude:  ClaudeClient,
    db:      DBLogger,
    tracker: PerformanceTracker,
) -> None:
    log.info("research_scan_start", focus=settings.market_focus)
    start_time = datetime.datetime.utcnow()

    # ── Fetch live crypto prices first ────────────────────────────────────────
    crypto_prices: dict = {}
    if settings.market_focus == "crypto":
        crypto_prices = await fetch_crypto_prices()
        # Use targeted 15min series queries — 3 API calls instead of 250+ pages
        markets = await kalshi.get_15min_markets()
        log.info("15min_markets_fetched", count=len(markets))
        if not markets:
            log.warning("no_15min_markets_found — falling back to broad crypto scan")
            markets = await kalshi.get_crypto_markets(min_volume=0)
    else:
        markets = await kalshi.get_all_open_markets()
        log.info("markets_fetched", count=len(markets))

    decisions: list[dict] = []
    ai_calls = 0

    # Classify markets into three tiers, then fill the 200-cap liquid-first so the
    # most actionable markets always get analyzed even when there are 45k+ total.
    all_fields   = [(m, extract_market_fields(m)) for m in markets]
    # Tier 1: liquid (volume >= threshold, price not at 0/1)
    liquid       = [m for m, f in all_fields
                    if f["yes_price"] not in (0.0, 1.0)
                    and f["volume"] >= settings.min_liquidity_dollars]
    # Tier 2: low-volume but price is live
    low_vol      = [m for m, f in all_fields
                    if f["yes_price"] not in (0.0, 1.0)
                    and 0 < f["volume"] < settings.min_liquidity_dollars]
    # Tier 3: zero-volume live markets (research only — likely untradeable)
    zero_vol     = [m for m, f in all_fields
                    if f["yes_price"] not in (0.0, 1.0)
                    and f["volume"] == 0]

    # Fill cap: liquid first, then low-vol, then zero-vol
    sampled = (liquid + low_vol + zero_vol)[: settings.research_max_markets]

    log.info(
        "research_filter",
        total_markets=len(markets),
        liquid=len(liquid),
        low_volume=len(low_vol),
        zero_volume=len(zero_vol),
        analyzing=len(sampled),
    )

    for market in sampled:
        fields = extract_market_fields(market)

        # Log market scan
        await db.log_market_scan(**fields, scan_mode="research")

        # Call Claude
        log.debug("claude_call_start", ticker=fields["ticker"], yes_price=f"{fields['yes_price']:.1%}", volume=fields["volume"])
        try:
            analysis = claude.analyze_market(
                market_id=fields["market_id"],
                ticker=fields["ticker"],
                title=fields["title"],
                category=fields["category"],
                yes_price=fields["yes_price"],
                no_price=fields["no_price"],
                volume=fields["volume"],
                close_time=fields["close_time"],
                crypto_prices=crypto_prices or None,
            )
            ai_calls += 1
            tradeable_score, expected_profit = compute_tradeable_score(
                edge=analysis.edge,
                confidence=analysis.confidence,
                volume=fields["volume"],
            )
            is_liquid    = fields["volume"] >= settings.min_liquidity_dollars
            is_zero_vol  = fields["volume"] == 0
            log.debug(
                "claude_call_ok",
                ticker=fields["ticker"],
                edge=f"{analysis.edge:+.1%}",
                rec=analysis.recommendation,
                expected_profit=f"${expected_profit:.2f}",
                tradeable_score=f"{tradeable_score:.3f}",
                liquid=is_liquid,
            )

            decision_id = await db.log_ai_decision(
                market_id=analysis.market_id,
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                claude_yes_probability=analysis.yes_probability,
                kalshi_yes_price=analysis.kalshi_yes_price,
                edge=analysis.edge,
                confidence=analysis.confidence,
                reasoning=analysis.reasoning,
                recommendation=analysis.recommendation,
                decision_mode="research",
                tokens_used=analysis.tokens_used,
                latency_ms=analysis.latency_ms,
            )

            decisions.append({
                "ticker":           analysis.ticker,
                "title":            analysis.market_title,
                "category":         fields["category"],
                "edge":             analysis.edge,
                "confidence":       analysis.confidence,
                "recommendation":   analysis.recommendation,
                "volume":           fields["volume"],
                "tradeable_score":  tradeable_score,
                "expected_profit":  expected_profit,
                "is_liquid":        is_liquid,
                "is_zero_volume":   is_zero_vol,
            })

            log.info(
                "market_analyzed",
                ticker=analysis.ticker,
                edge=f"{analysis.edge:+.1%}",
                confidence=f"{analysis.confidence:.0%}",
                recommendation=analysis.recommendation,
                expected_profit=f"${expected_profit:.2f}",
                tradeable_score=f"{tradeable_score:.3f}",
                volume=f"${fields['volume']:,.0f}",
                liquid=is_liquid,
            )

            # Discord: only notify on actionable decisions to avoid spam
            if analysis.recommendation != "SKIP":
                await discord.notify_ai_decision(
                    ticker=analysis.ticker,
                    title=analysis.market_title,
                    recommendation=analysis.recommendation,
                    edge=analysis.edge,
                    confidence=analysis.confidence,
                    kalshi_price=analysis.kalshi_yes_price,
                    claude_prob=analysis.yes_probability,
                    reasoning=analysis.reasoning,
                    mode="research",
                    expected_profit=expected_profit,
                    tradeable_score=tradeable_score,
                    volume=fields["volume"],
                    coin=analysis.coin,
                    timeframe=analysis.timeframe,
                    live_price=analysis.live_price,
                    strike_price=analysis.strike_price,
                    direction=analysis.direction,
                    change_24h=crypto_prices.get(analysis.coin, {}).get("change_24h", 0),
                )
                # Record to performance log (research = $0 wagered, WIN/LOSS tracked for accuracy)
                rec_side = "yes" if analysis.recommendation == "BUY_YES" else "no"
                await tracker.record(
                    analysis=analysis,
                    side=rec_side,
                    contracts=0,
                    bet_dollars=0.0,
                    mode="research",
                )
                # Write to paper trading journal
                journal_mod.log_trade_placed(
                    analysis=analysis,
                    decision={"side": rec_side, "contracts": 0, "bet_dollars": 0.0},
                    mode="research",
                    crypto_prices=crypto_prices,
                )

        except Exception as exc:
            log.error("claude_error", ticker=fields["ticker"], error=str(exc))
            await _report_error(exc, f"Analysis error on {fields['ticker']}")

        # Throttle to avoid rate limits
        await asyncio.sleep(settings.research_scan_delay_seconds)

    # ── Build strategy report ────────────────────────────────────────────────
    end_time = datetime.datetime.utcnow()
    category_stats = compute_category_stats(decisions)
    top_3 = category_stats[:3]

    # Recommend funding = max_bet * max_positions * 2 (buffer)
    recommended_funding = settings.max_bet_dollars * settings.max_open_positions * 2
    actionable = [d for d in decisions if d["recommendation"] != "SKIP"]
    liquid_actionable = [d for d in actionable if d.get("is_liquid")]
    zero_vol_count = sum(1 for d in decisions if d.get("is_zero_volume"))
    overall_edge = (
        sum(abs(d["edge"]) for d in actionable)
        / max(1, len(actionable))
    )
    overall_score = (
        sum(d.get("tradeable_score", 0) for d in actionable)
        / max(1, len(actionable))
    )

    report = {
        "report_date": datetime.date.today().isoformat(),
        "research_start": start_time.isoformat(),
        "research_end": end_time.isoformat(),
        "markets_analyzed": len(decisions),
        "categories_analyzed": json.dumps(category_stats),
        "top_categories": json.dumps(top_3),
        "recommended_bet_size": settings.max_bet_dollars,
        "recommended_funding": recommended_funding,
        "overall_edge": round(overall_edge, 4),
        "summary": (
            f"Scanned {len(markets)} markets, analyzed {len(decisions)}. "
            f"Liquid+actionable: {len(liquid_actionable)}. "
            f"Zero-volume flagged: {zero_vol_count}. "
            f"Average edge: {overall_edge:.1%}. Average tradeable score: {overall_score:.3f}. "
            f"Top category: {top_3[0]['category'] if top_3 else 'N/A'}."
        ),
        "full_report": json.dumps({"decisions": decisions, "by_category": category_stats}),
    }
    await db.save_strategy_report(report)

    duration_minutes = (end_time - start_time).total_seconds() / 60
    log.info(
        "research_scan_complete",
        markets_scanned=len(markets),
        ai_calls=ai_calls,
        top_category=top_3[0]["category"] if top_3 else "none",
        recommended_funding=f"${recommended_funding:,.0f}",
    )

    await discord.notify_research_complete(
        markets_scanned=len(markets),
        markets_analyzed=len(decisions),
        liquid_actionable=len(liquid_actionable),
        zero_volume_flagged=zero_vol_count,
        overall_edge=overall_edge,
        overall_score=overall_score,
        top_categories=top_3,
        recommended_funding=recommended_funding,
        duration_minutes=duration_minutes,
    )

    print("\n" + "="*60)
    print("RESEARCH REPORT COMPLETE")
    print(f"Markets analyzed: {len(decisions)}")
    print(f"Overall edge: {overall_edge:.1%}")
    print(f"Recommended funding: ${recommended_funding:,.0f}")
    print(f"Top categories:")
    for c in top_3:
        print(f"  {c['category']}: avg edge {c['edge_avg']:.1%}, n={c['sample_size']}")
    print("="*60 + "\n")


# ── Live Mode ─────────────────────────────────────────────────────────────────

async def run_live_scan(
    kalshi:       KalshiClient,
    claude:       ClaudeClient,
    db:           DBLogger,
    order_mgr:    OrderManager,
    tracker:      PerformanceTracker,
    period_stats: "_PeriodStats | None" = None,
) -> None:
    global _current_window_close, _analyzed_this_window, _use_fallback_model, _credits_exhausted

    log.info("live_scan_start", demo=settings.demo_mode, focus=settings.market_focus)

    # Resolve any markets that closed since the last cycle
    try:
        newly_resolved = await tracker.resolve_pending(kalshi)
        if newly_resolved:
            log.info("markets_resolved", count=len(newly_resolved))
            for resolved_row in newly_resolved:
                journal_mod.log_trade_resolved(resolved_row)
                # Discord WIN/LOSS notification
                try:
                    pnl  = float(resolved_row.get("Profit/Loss ($)") or 0)
                    won  = resolved_row.get("Result") == "WIN"
                    mode_row = resolved_row.get("Mode", "RESEARCH").upper()
                    import journal as _j
                    _state  = _j._read_state()
                    running = _state.get("running_pnl", 0.0)
                    balance = _state.get("starting_balance", 500.0) + running
                    await discord.notify_trade_resolved(
                        ticker=resolved_row.get("_ticker", resolved_row.get("Ticker", "?")),
                        market_title=resolved_row.get("Market Title", ""),
                        won=won,
                        pnl=pnl,
                        running_pnl=running,
                        wins=_state.get("wins", 0),
                        losses=_state.get("losses", 0),
                        balance=balance,
                        mode=mode_row,
                    )
                except Exception as _exc:
                    log.warning("discord_resolve_notify_failed", error=str(_exc))
            # Issue 7: after all resolutions, send a position update
            try:
                pending_after = tracker.get_pending_trades()
                await discord.notify_position_update(pending_after)
            except Exception as _exc:
                log.warning("position_update_notify_failed", error=str(_exc))
    except Exception as exc:
        log.warning("resolve_pending_failed", error=str(exc))

    # Sync existing order fills (skip in paper mode — auth not needed, no real orders)
    if not settings.paper_trading:
        await order_mgr.sync_open_orders()
        await order_mgr.cancel_stale_orders()

    # Get current risk state
    daily_loss     = await db.get_today_loss()
    open_positions = await db.get_open_position_count()

    # Fetch live prices + targeted market list
    crypto_prices: dict = {}
    if settings.market_focus == "crypto":
        crypto_prices = await fetch_crypto_prices(window_close=_current_window_close)
        markets = await kalshi.get_15min_markets()
        log.info("15min_markets_fetched", count=len(markets))
        if not markets:
            if settings.paper_trading:
                # In paper mode, skip the cycle if all 15min windows have expired
                # rather than paginating 50k markets and hitting rate limits
                log.info("no_15min_markets_available — waiting for next window")
                return
            log.warning("no_15min_markets_found — falling back to broad crypto scan")
            try:
                markets = await kalshi.get_crypto_markets(min_volume=settings.min_liquidity_dollars)
            except Exception as exc:
                log.warning("fallback_crypto_scan_failed", error=str(exc))
                return
    else:
        markets = await kalshi.get_all_open_markets()
        log.info("markets_fetched", count=len(markets))
    if period_stats:
        period_stats.markets_scanned += len(markets)

    # ── Issue 1 & 2: Window detection — detect new 15min window ─────────────
    if markets:
        window_close = markets[0].get("close_time", "")
        if window_close and window_close != _current_window_close:
            log.info(
                "new_window_detected",
                old_close=_current_window_close or "none",
                new_close=window_close,
                clearing=len(_analyzed_this_window),
            )
            _current_window_close = window_close
            _analyzed_this_window.clear()

    trades_placed  = 0
    volume_skipped = 0
    filter_skipped = 0
    active_coins   = _get_active_coins()

    for market in markets:
        fields = extract_market_fields(market)

        # Dedup: skip if already analyzed this window
        if fields["ticker"] in _analyzed_this_window:
            log.debug("skip_already_analyzed_this_window", ticker=fields["ticker"])
            continue

        # ── Pre-filter 1: Active coins ───────────────────────────────────────
        market_coin = next((c for c in ("BTC", "ETH", "SOL") if c in fields["ticker"].upper()), None)
        if market_coin and market_coin not in active_coins:
            log.debug("skip_inactive_coin", ticker=fields["ticker"], coin=market_coin, active=active_coins)
            continue

        # ── Pre-filter 2: Volume — configurable floor before calling Claude ────
        if fields["volume"] < settings.volume_floor:
            log.debug("skip_low_volume", ticker=fields["ticker"], volume=f"${fields['volume']:.0f}", floor=settings.volume_floor)
            volume_skipped += 1
            continue

        # ── Pre-filter 3: Price range — coin flip / already decided ──────────
        yp = fields["yes_price"]
        if 0.44 <= yp <= 0.56:
            log.debug("skip_coin_flip", ticker=fields["ticker"], yes_price=f"{yp:.1%}")
            filter_skipped += 1
            continue
        if yp > 0.88 or yp < 0.12:
            log.debug("skip_market_decided", ticker=fields["ticker"], yes_price=f"{yp:.1%}")
            filter_skipped += 1
            continue

        # ── Pre-filter 4: Max possible edge — skip when price is near 50% ────
        if abs(yp - 0.50) < 0.08:
            log.debug("skip_max_edge_insufficient", ticker=fields["ticker"], distance=f"{abs(yp-0.50):.1%}")
            filter_skipped += 1
            continue

        # Skip ALL Claude calls while credits are exhausted — main loop will sleep 30 min
        if _credits_exhausted:
            break

        # Mark as analyzed for this window before the Claude call so we never
        # double-call on a retry loop or exception
        _analyzed_this_window.add(fields["ticker"])

        await db.log_market_scan(**fields, scan_mode="live")

        try:
            # Use fallback (Haiku) model when daily cost limit has been reached
            model_override = settings.claude_fallback_model if _use_fallback_model else None

            ta_context = _ta.get_ta_context(market_coin) if (_ta and market_coin) else ""
            analysis = claude.analyze_market(
                market_id=fields["market_id"],
                ticker=fields["ticker"],
                title=fields["title"],
                category=fields["category"],
                yes_price=fields["yes_price"],
                no_price=fields["no_price"],
                volume=fields["volume"],
                close_time=fields["close_time"],
                crypto_prices=crypto_prices or None,
                model_override=model_override,
                ta_context=ta_context,
            )

            # Record API cost and check daily limits
            await _record_api_cost(
                cost=analysis.cost_dollars,
                is_fallback=_use_fallback_model,
                db=db,
            )

            # Credit restoration detection — if we were in error state, post "Back Online"
            await _note_api_success()

            decision_id = await db.log_ai_decision(
                market_id=analysis.market_id,
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                claude_yes_probability=analysis.yes_probability,
                kalshi_yes_price=analysis.kalshi_yes_price,
                edge=analysis.edge,
                confidence=analysis.confidence,
                reasoning=analysis.reasoning,
                recommendation=analysis.recommendation,
                decision_mode="live",
                tokens_used=analysis.tokens_used,
                latency_ms=analysis.latency_ms,
            )

            engine = DecisionEngine(
                current_positions=open_positions,
                daily_loss_dollars=daily_loss,
            )
            decision = engine.evaluate(analysis, volume=fields["volume"])

            if period_stats:
                period_stats.ai_calls += 1

            live_score, live_profit = compute_tradeable_score(
                edge=analysis.edge,
                confidence=analysis.confidence,
                volume=fields["volume"],
            )
            # Discord: notify on all live decisions (actionable or not)
            await discord.notify_ai_decision(
                ticker=analysis.ticker,
                title=analysis.market_title,
                recommendation=analysis.recommendation,
                edge=analysis.edge,
                confidence=analysis.confidence,
                kalshi_price=analysis.kalshi_yes_price,
                claude_prob=analysis.yes_probability,
                reasoning=analysis.reasoning,
                mode="live",
                expected_profit=live_profit,
                tradeable_score=live_score,
                volume=fields["volume"],
                coin=analysis.coin,
                timeframe=analysis.timeframe,
                live_price=analysis.live_price,
                strike_price=analysis.strike_price,
                direction=analysis.direction,
                change_24h=crypto_prices.get(analysis.coin, {}).get("change_24h", 0),
            )

            # Accumulate top opportunities for the periodic summary
            if period_stats and analysis.recommendation != "SKIP":
                period_stats.opportunities.append({
                    "ticker": analysis.ticker,
                    "title": analysis.market_title,
                    "recommendation": analysis.recommendation,
                    "edge": analysis.edge,
                    "confidence": analysis.confidence,
                })
                if abs(analysis.edge) > 0.20:
                    period_stats.notable_edges += 1

            if decision["approved"]:
                # Record to performance log before placing the order
                await tracker.record(
                    analysis=analysis,
                    side=decision["side"],
                    contracts=decision["contracts"],
                    bet_dollars=decision["bet_dollars"],
                    mode="live",
                )
                # Write to trading journal
                journal_mod.log_trade_placed(
                    analysis=analysis,
                    decision=decision,
                    mode="live",
                    crypto_prices=crypto_prices,
                )
                order = await order_mgr.execute_trade(
                    analysis, decision, decision_id,
                    close_time=fields["close_time"],
                    volume=fields["volume"],
                )
                if order:
                    trades_placed += 1
                    open_positions += 1  # Optimistically increment
                    daily_loss += decision["bet_dollars"]  # Pessimistically track cost
                    if period_stats:
                        period_stats.trades_placed += 1

        except Exception as exc:
            log.error("scan_error", ticker=fields["ticker"], error=str(exc))
            if _is_credit_error(exc):
                # Credits are account-level — post the one-time alert, pause the bot
                _credits_exhausted = True
                await _report_error(exc, "Market analysis")  # posts once, then silenced
                log.warning("credits_exhausted_stopping_scan_cycle")
                break  # stop analyzing markets; main loop will sleep 30 min
            else:
                await _report_error(exc, f"Scan error on {fields['ticker']}")

        await asyncio.sleep(0.5)  # Gentle throttle

    log.info(
        "live_scan_complete",
        trades_placed=trades_placed,
        volume_skipped=volume_skipped,
        filter_skipped=filter_skipped,
        daily_cost=f"${_daily_cost_dollars:.4f}",
        calls_today=_daily_cost_calls,
        economy_mode=_use_fallback_model,
    )


# ── Periodic summary task ──────────────────────────────────────────────────────

class _PeriodStats:
    """Mutable counters shared between the live scan loop and the summary task."""
    def __init__(self) -> None:
        self.markets_scanned:  int = 0
        self.ai_calls:         int = 0
        self.trades_placed:    int = 0
        self.notable_edges:    int = 0   # abs(edge) > 0.20
        self.wins:             int = 0   # trades resolved WIN this period
        self.losses:           int = 0   # trades resolved LOSS this period
        self.opportunities:    list[dict] = []  # best BUY decisions seen

    def reset(self) -> None:
        self.markets_scanned = 0
        self.ai_calls = 0
        self.trades_placed = 0
        self.notable_edges = 0
        self.wins = 0
        self.losses = 0
        self.opportunities = []


async def _periodic_summary_task(
    stats: _PeriodStats,
    tracker: "PerformanceTracker",
    interval_hours: int = 6,
) -> None:
    """Runs forever, firing a Discord summary every `interval_hours` hours."""
    interval_secs = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_secs)

        # Skip summary silently when credits are exhausted (we already posted an alert)
        if _credits_exhausted:
            log.info("periodic_summary_skipped_credits_exhausted")
            stats.reset()
            continue

        # Issue 4: credit alert if markets were scanned but Claude was never called
        # (only fire when not already in exhausted state — that's handled above)
        if stats.markets_scanned > 0 and stats.ai_calls == 0 and not _credits_exhausted:
            await discord.notify_credit_alert(
                markets_scanned=stats.markets_scanned,
                period_hours=interval_hours,
            )
            log.warning("credit_alert_sent", markets_scanned=stats.markets_scanned)
            stats.reset()
            continue

        # Issue 1: skip silent periods (nothing happened)
        if stats.trades_placed == 0 and stats.wins == 0 and stats.losses == 0 and stats.notable_edges == 0:
            log.info("periodic_summary_skipped_quiet", interval_hours=interval_hours)
            stats.reset()
            continue

        # Issue 3: read cumulative record from journal
        import journal as _j
        _state       = _j._read_state()
        running_pnl  = _state.get("running_pnl", 0.0)
        starting_bal = _state.get("starting_balance", 500.0)
        balance      = starting_bal + running_pnl
        total_wins   = _state.get("wins", 0)
        total_losses = _state.get("losses", 0)

        # Sort by absolute edge and take top 5
        top = sorted(stats.opportunities, key=lambda x: abs(x.get("edge", 0)), reverse=True)[:5]

        await discord.notify_periodic_summary(
            period_hours=interval_hours,
            markets_scanned=stats.markets_scanned,
            ai_calls=stats.ai_calls,
            trades_placed=stats.trades_placed,
            top_opportunities=top,
            demo=settings.demo_mode,
            balance=balance,
            running_pnl=running_pnl,
            total_wins=total_wins,
            total_losses=total_losses,
        )
        log.info("periodic_summary_sent", interval_hours=interval_hours)
        stats.reset()


# ── Resolution checker task (paper trading) ───────────────────────────────────

async def _resolution_checker_task(
    tracker: PerformanceTracker,
    kalshi:  KalshiClient,
    interval_minutes: int = 5,
    period_stats: "_PeriodStats | None" = None,
) -> None:
    """
    Background task for paper trading mode.
    Polls every `interval_minutes` for newly-resolved markets,
    logs WIN/LOSS to journal, and fires Discord notifications.
    """
    interval_secs = interval_minutes * 60
    while True:
        await asyncio.sleep(interval_secs)
        try:
            newly_resolved = await tracker.resolve_pending(kalshi)
            if not newly_resolved:
                continue
            log.info("paper_resolution_check", resolved=len(newly_resolved))
            for resolved_row in newly_resolved:
                journal_mod.log_trade_resolved(resolved_row)
                try:
                    pnl  = float(resolved_row.get("Profit/Loss ($)") or 0)
                    won  = resolved_row.get("Result") == "WIN"
                    if period_stats:
                        if won:
                            period_stats.wins += 1
                        else:
                            period_stats.losses += 1
                    import journal as _j
                    _state  = _j._read_state()
                    running = _state.get("running_pnl", 0.0)
                    balance = _state.get("starting_balance", 500.0) + running
                    await discord.notify_trade_resolved(
                        ticker=resolved_row.get("_ticker", resolved_row.get("Ticker", "?")),
                        market_title=resolved_row.get("Market Title", ""),
                        won=won,
                        pnl=pnl,
                        running_pnl=running,
                        wins=_state.get("wins", 0),
                        losses=_state.get("losses", 0),
                        balance=balance,
                        mode=resolved_row.get("Mode", "PAPER").upper(),
                    )
                except Exception as _exc:
                    log.warning("paper_resolve_notify_failed", error=str(_exc))
            # Issue 7: follow-up position update after all resolutions
            try:
                pending_after = tracker.get_pending_trades()
                await discord.notify_position_update(pending_after)
            except Exception as _exc:
                log.warning("position_update_after_resolve_failed", error=str(_exc))
        except Exception as exc:
            log.warning("resolution_checker_failed", error=str(exc))


# ── Daily performance report task ─────────────────────────────────────────────

async def _daily_performance_task(
    tracker: PerformanceTracker,
    kalshi:  KalshiClient,
    interval_hours: int = 24,
) -> None:
    """
    Background task: every 24 hours, resolve pending markets, rebuild the XLSX,
    and send the updated spreadsheet to Discord with a performance embed.
    """
    interval_secs = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_secs)
        try:
            resolved = await tracker.resolve_pending(kalshi)
            today_stats    = tracker.get_stats(today_only=True)
            alltime_stats  = tracker.get_stats(today_only=False)
            xlsx_bytes     = tracker.get_xlsx_bytes()
            await discord.notify_daily_performance(
                today_stats=today_stats,
                all_time_stats=alltime_stats,
                xlsx_bytes=xlsx_bytes,
                demo=settings.demo_mode,
                resolved_count=len(resolved),
            )
            # Daily cost report
            await discord.notify_daily_cost_report(
                date=datetime.date.today().isoformat(),
                total_cost=_daily_cost_dollars,
                total_calls=_daily_cost_calls,
                trades_placed=today_stats.get("total_trades", 0),
                model_primary=settings.claude_model,
                model_fallback=settings.claude_fallback_model,
                fallback_calls=_daily_fallback_calls,
            )
            log.info(
                "daily_performance_sent",
                resolved=len(resolved),
                total_trades=alltime_stats.get("total_trades"),
                total_pnl=alltime_stats.get("total_pnl"),
                daily_cost=f"${_daily_cost_dollars:.4f}",
                daily_calls=_daily_cost_calls,
            )
        except Exception as exc:
            log.error("daily_performance_task_failed", error=str(exc))
            await _report_error(exc, "Daily performance report")


# ── Technical Analysis refresh task ──────────────────────────────────────────

# Module-level TA instance — shared across tasks
_ta: "TechnicalAnalyzer | None" = None

async def _ta_refresh_task(ta: TechnicalAnalyzer, interval_minutes: int = 15) -> None:
    """Refresh Binance candles every interval_minutes. First run is immediate."""
    global _ta
    try:
        await ta.refresh_all()
        _ta = ta
        log.info("ta_initial_refresh_complete")
    except Exception as exc:
        log.warning("ta_initial_refresh_failed", error=str(exc))

    interval_secs = interval_minutes * 60
    while True:
        await asyncio.sleep(interval_secs)
        try:
            await ta.refresh_all()
            _ta = ta
            log.info("ta_refresh_complete", age_min=round(ta.cache_age_minutes, 1))
        except Exception as exc:
            log.warning("ta_refresh_failed", error=str(exc))


async def _ta_summary_task(ta: TechnicalAnalyzer, interval_hours: int = 1) -> None:
    """Post combined Market Pulse for all coins to #analysis once per hour."""
    interval_secs = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_secs)
        if ta.cache_age_minutes > 90:
            log.debug("ta_summary_skipped_stale_cache")
            continue
        try:
            pulse = ta.get_combined_market_pulse()
            if pulse:
                await discord.notify_market_pulse(pulse)
                log.info("ta_market_pulse_posted")
        except Exception as exc:
            log.warning("ta_summary_post_failed", error=str(exc))


# ── News Intelligence task ────────────────────────────────────────────────────

async def _news_intelligence_task(
    news: NewsIntelligence,
    kalshi: "KalshiClient",
    interval_minutes: int = 30,
) -> None:
    """Scan news and match to Kalshi markets every interval_minutes."""
    interval_secs = interval_minutes * 60

    async def _post_thesis(thesis, markets, headline):
        await discord.notify_news_thesis(thesis, markets, headline)

    async def _post_breaking(article, markets):
        await discord.notify_breaking_news(article, markets)

    async def _post_calendar(events, markets):
        await discord.notify_economic_calendar(events, markets)

    while True:
        await asyncio.sleep(interval_secs)
        try:
            actions = await news.scan(_post_thesis, _post_breaking, _post_calendar)
            log.info("news_intelligence_scan_complete", actions=actions)
        except Exception as exc:
            log.warning("news_intelligence_scan_failed", error=str(exc))


# ── Scheduled analysis task (briefings + weekly) ──────────────────────────────

async def _scheduled_analysis_task(
    ta: TechnicalAnalyzer,
    tracker: "PerformanceTracker",
    kalshi: "KalshiClient",
    model_override_fn=None,  # callable() → str | None, checked at call time
) -> None:
    """
    Polls every 15 minutes for scheduled events:
    - Daily briefing at 7am ET (noon UTC)
    - Weekly analysis every Monday at 8am ET
    """
    while True:
        await asyncio.sleep(900)  # check every 15 minutes
        try:
            # ── Daily briefing ─────────────────────────────────────────────
            model_ov = model_override_fn() if model_override_fn else None

            if should_post_daily_briefing():
                try:
                    resp = await kalshi._get("/markets", params={"status": "open", "limit": 100})
                    markets = resp.get("markets", [])
                    macro   = await get_macro_data()
                    ta_sums = ta._cache  # already refreshed by _ta_refresh_task
                    content = await build_daily_briefing(
                        crypto_prices=_price_cache,
                        ta_summaries=ta_sums,
                        kalshi_markets=markets,
                        macro_data=macro,
                        model_override=model_ov,
                    )
                    await discord.notify_daily_briefing(content)
                    mark_briefing_posted()
                    log.info("daily_briefing_posted")
                except Exception as exc:
                    log.warning("daily_briefing_failed", error=str(exc))

            # ── Weekly analysis ────────────────────────────────────────────
            if should_post_weekly():
                try:
                    resp = await kalshi._get("/markets", params={"status": "open", "limit": 100})
                    markets   = resp.get("markets", [])
                    all_stats = tracker.get_stats(today_only=False)
                    ta_sums   = ta._cache
                    content   = await build_weekly_analysis(
                        perf_stats=all_stats,
                        crypto_prices=_price_cache,
                        ta_summaries=ta_sums,
                        kalshi_markets=markets,
                        model_override=model_ov,
                    )
                    await discord.notify_weekly_analysis(content)
                    mark_weekly_posted()
                    log.info("weekly_analysis_posted")
                except Exception as exc:
                    log.warning("weekly_analysis_failed", error=str(exc))

        except Exception as exc:
            log.warning("scheduled_analysis_task_error", error=str(exc))


# ── Gmail Morning Brief task ──────────────────────────────────────────────────

async def _gmail_brief_task(
    gmail: GmailReader,
    kalshi: "KalshiClient",
    model_override_fn=None,  # callable() → str | None
) -> None:
    """
    Posts the morning brief once per day between 5:30–9:00am ET (10:30–14:00 UTC).
    Checks every 10 minutes. Posts as soon as any newsletters are found.
    At 9:00am ET the window closes -- whatever arrived by then is used.
    Zero Claude calls if no newsletters arrive at all.
    """
    # Build sender list -- prefer NEWSLETTER_EMAILS (multi), fall back to NEWSLETTER_EMAIL
    raw = getattr(settings, "newsletter_emails", "") or getattr(settings, "newsletter_email", "")
    senders = [s.strip() for s in raw.split(",") if s.strip()]
    if not senders:
        log.debug("[brief_task] no newsletter senders configured -- skipping")
        return

    log.info("[brief_task] watching %d sender(s): %s", len(senders), ", ".join(senders))

    while True:
        await asyncio.sleep(600)  # check every 10 minutes

        now_utc = datetime.datetime.utcnow()
        # 5:30–9:00am ET == 10:30–14:00 UTC
        in_window = (
            (now_utc.hour == 10 and now_utc.minute >= 30)
            or (11 <= now_utc.hour < 14)
        )
        if not in_window:
            continue

        if gmail.already_posted_today():
            continue

        try:
            resp    = await kalshi._get("/markets", params={"status": "open", "limit": 100})
            markets = resp.get("markets", [])
        except Exception as exc:
            log.warning("[brief_task] failed to fetch markets: %s", exc)
            markets = []

        try:
            newsletters = await gmail.fetch_all_newsletters(senders)

            # Before the 9am deadline: wait for at least one newsletter to arrive.
            # At/after 8:50am ET (13:50 UTC): post with whatever we have.
            at_deadline = now_utc.hour == 13 and now_utc.minute >= 50
            if not newsletters:
                if at_deadline:
                    log.info("[brief_task] 9am deadline reached with no newsletters -- skipping today")
                    gmail.mark_posted()  # prevent further checks today
                else:
                    log.debug("[brief_task] no newsletters found yet (%d senders checked)", len(senders))
                continue

            log.info("[brief_task] %d newsletter(s) found: %s", len(newsletters), list(newsletters.keys()))
            model_ov = model_override_fn() if model_override_fn else None
            brief, cost = await build_morning_brief(newsletters, markets, model_override=model_ov)

            await discord.notify_morning_brief(brief)

            focus = extract_todays_focus(brief)
            if focus:
                await discord.notify_todays_focus(focus)

            gmail.mark_posted()
            log.info("[brief_task] morning brief posted (%d sources), cost=$%.4f", len(newsletters), cost)

        except Exception as exc:
            log.warning("[brief_task] failed: %s", exc)


# ── Axios breaking news alerts task ───────────────────────────────────────────

async def _axios_alerts_task(
    gmail: GmailReader,
    kalshi: "KalshiClient",
    model_override_fn=None,
) -> None:
    """
    Check for new Axios breaking news alerts every 30 minutes throughout the day.
    Evaluates market impact via one Claude call per alert.
    Posts to #breaking-news. Max MAX_BREAKING_PER_DAY posts per day.
    """
    while True:
        await asyncio.sleep(1800)  # 30 minutes

        if not gmail.is_configured:
            continue

        today = datetime.date.today().isoformat()

        # Check daily cap before making any IMAP connection
        if _er._breaking_date == today and _er._breaking_count >= _er.MAX_BREAKING_PER_DAY:
            log.debug("[axios-alerts] daily cap reached (%d), skipping check", _er.MAX_BREAKING_PER_DAY)
            continue

        try:
            alerts = await gmail.fetch_recent_axios_alerts()
            if not alerts:
                continue

            log.info("[axios-alerts] %d new alert(s) to evaluate", len(alerts))

            try:
                resp    = await kalshi._get("/markets", params={"status": "open", "limit": 100})
                markets = resp.get("markets", [])
            except Exception as exc:
                log.warning("[axios-alerts] failed to fetch markets: %s", exc)
                markets = []

            model_ov = model_override_fn() if model_override_fn else None

            for alert in alerts:
                # Re-check cap inside the batch
                if _er._breaking_date == today and _er._breaking_count >= _er.MAX_BREAKING_PER_DAY:
                    log.info("[axios-alerts] daily cap (%d) hit mid-batch, stopping", _er.MAX_BREAKING_PER_DAY)
                    break

                try:
                    post, cost = await evaluate_axios_alert(
                        alert, markets,
                        settings.anthropic_api_key,
                        model_ov or settings.claude_model,
                    )

                    if cost > 0:
                        log.info("[axios-alerts] alert eval cost=$%.4f", cost)

                    if post:
                        await discord.notify_breaking_alert(post)
                        if _er._breaking_date != today:
                            _er._breaking_count = 0
                            _er._breaking_date  = today
                        _er._breaking_count += 1
                        log.info("[axios-alerts] posted alert #%d today", _er._breaking_count)

                except Exception as exc:
                    log.warning("[axios-alerts] alert evaluation failed: %s", exc)

        except Exception as exc:
            log.warning("[axios-alerts] check failed: %s", exc)


# ── Market Intelligence task ──────────────────────────────────────────────────

def _update_scanner_prices(scanner: "IntelligenceScanner") -> None:
    """Push latest TA close prices into the scanner's live_prices dict."""
    if _ta is None:
        return
    prices: dict[str, float] = {}
    for coin in ("BTC", "ETH", "SOL"):
        ind = _ta.get_indicators(coin).get("1h", {})
        p = ind.get("price")
        if p:
            prices[coin] = float(p)
    if prices:
        scanner.live_prices = prices


async def _intelligence_scan_task(
    scanner: "IntelligenceScanner",
    interval_minutes: int = 30,
) -> None:
    """
    Background task: runs an intelligence scan immediately, then every
    `interval_minutes`. Errors are caught so the trading loop never dies.
    """
    # First scan fires immediately
    try:
        _update_scanner_prices(scanner)
        alerts = await scanner.scan()
        log.info("intelligence_scan_startup", alerts=alerts)
    except Exception as exc:
        log.warning("intelligence_scan_startup_failed", error=str(exc))

    interval_secs = interval_minutes * 60
    while True:
        await asyncio.sleep(interval_secs)
        try:
            _update_scanner_prices(scanner)
            alerts = await scanner.scan()
            log.info("intelligence_scan_cycle", alerts=alerts, interval_min=interval_minutes)
        except Exception as exc:
            log.warning("intelligence_scan_failed", error=str(exc))


# ── Credit check task ────────────────────────────────────────────────────────

async def _credit_check_task() -> None:
    """
    When credits are exhausted, probe the API every 30 minutes.
    On success, auto-restore and post 'Credits restored' to #alerts.
    """
    import anthropic as _anthro
    while True:
        await asyncio.sleep(_CREDIT_PAUSE_SECS)
        if not _credits_exhausted:
            continue
        try:
            client = _anthro.Anthropic(api_key=settings.anthropic_api_key)
            client.messages.create(
                model=settings.claude_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            # Success — credits restored
            await _note_api_success()
            log.info("credits_restored_auto_check")
        except Exception as exc:
            if _is_credit_error(exc):
                log.info("credits_still_exhausted_retry_in_30min")
            else:
                log.warning("credit_check_probe_failed", error=str(exc))


# ── Economic calendar task ────────────────────────────────────────────────────

async def _economic_calendar_task(kalshi: "KalshiClient") -> None:
    """
    Two jobs:
      - Sunday 8am ET (13:00 UTC): post week-ahead calendar to #economic-calendar
      - Weekday 7am ET (12:00 UTC): post daily alert to #morning-brief if HIGH events today
    Checks every 5 minutes. Zero Claude calls.
    """
    fred_key     = getattr(settings, "fred_api_key", "")
    finnhub_key  = getattr(settings, "finnhub_api_key", "")

    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        try:
            # ── Sunday weekly calendar ────────────────────────────────────────
            if _is_sunday_8am_et() and not weekly_already_posted():
                try:
                    resp    = await kalshi._get("/markets", params={"status": "open", "limit": 100})
                    markets = resp.get("markets", [])
                except Exception:
                    markets = []
                try:
                    content = await post_weekly_calendar(finnhub_key, fred_key, markets)
                    await discord.notify_economic_calendar_weekly(content)
                    mark_weekly_posted()
                    log.info("[calendar_task] weekly calendar posted")
                except Exception as exc:
                    log.warning("[calendar_task] weekly post failed: %s", exc)

            # ── Weekday 7am daily alert ────────────────────────────────────────
            if _is_weekday_7am_et() and not daily_alert_already_posted():
                try:
                    alert = await get_daily_calendar_alert(finnhub_key)
                    if alert:
                        await discord.notify_daily_calendar_alert(alert)
                        log.info("[calendar_task] daily alert posted: %s", alert[:80])
                    mark_daily_alert_posted()
                except Exception as exc:
                    log.warning("[calendar_task] daily alert failed: %s", exc)

        except Exception as exc:
            log.warning("[calendar_task] error: %s", exc)


# ── Market open task ──────────────────────────────────────────────────────────

async def _market_open_task() -> None:
    """
    Posts a market open snapshot to #market-open at 9:30am ET (13:30 UTC).
    Checks every 5 minutes. Zero Claude calls.
    """
    fred_key    = getattr(settings, "fred_api_key", "")
    finnhub_key = getattr(settings, "finnhub_api_key", "")
    av_key      = getattr(settings, "alpha_vantage_key", "")

    while True:
        await asyncio.sleep(300)
        try:
            now_utc = datetime.datetime.utcnow()
            # 9:30am ET = 13:30 UTC (summer) — check 13:25–13:59 window, weekdays only
            is_open_time = (
                now_utc.weekday() < 5
                and now_utc.hour == 13
                and now_utc.minute >= 25
            )
            if not is_open_time:
                continue
            if market_open_already_posted():
                continue

            content = await build_market_open(fred_key, finnhub_key, av_key)
            await discord.notify_market_open(content)
            mark_open_posted()
            log.info("[open_task] market open posted")

        except Exception as exc:
            log.warning("[open_task] failed: %s", exc)


# ── Market close task ─────────────────────────────────────────────────────────

async def _market_close_task(model_override_fn=None) -> None:
    """
    Posts the market close reflection to #market-close at 4:00pm ET (20:00 UTC).
    ONE Claude call per day. Checks every 5 minutes.
    """
    fred_key    = getattr(settings, "fred_api_key", "")
    finnhub_key = getattr(settings, "finnhub_api_key", "")
    av_key      = getattr(settings, "alpha_vantage_key", "")
    sb_url      = getattr(settings, "supabase_url", "")
    sb_key      = getattr(settings, "supabase_service_role_key", "")

    while True:
        await asyncio.sleep(300)
        try:
            now_utc = datetime.datetime.utcnow()
            # 4:00pm ET = 20:00 UTC (summer) — check 19:55–20:59, weekdays only
            is_close_time = (
                now_utc.weekday() < 5
                and now_utc.hour == 20
                and now_utc.minute >= 0
            )
            if not is_close_time:
                continue
            if market_close_already_posted():
                continue

            model_ov = model_override_fn() if model_override_fn else None
            content, cost = await build_market_close(
                fred_api_key=fred_key,
                finnhub_api_key=finnhub_key,
                av_key=av_key,
                supabase_url=sb_url,
                supabase_key=sb_key,
                anthropic_api_key=settings.anthropic_api_key,
                claude_model=settings.claude_model,
                model_override=model_ov,
            )
            await discord.notify_market_close(content)
            mark_close_posted()
            log.info("[close_task] market close posted cost=$%.4f", cost)

        except Exception as exc:
            log.warning("[close_task] failed: %s", exc)


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main_async() -> None:
    import os
    from pathlib import Path as _Path

    if settings.paper_trading:
        mode = "paper"
    elif settings.research_mode:
        mode = "research"
    else:
        mode = "live"
    log.info("kal_starting", mode=mode, demo=settings.demo_mode, paper=settings.paper_trading)

    # ── Lock file: kill any existing instance before starting ────────────────
    _lock_path = _Path(__file__).parent / "kal.lock"
    _my_pid    = os.getpid()
    try:
        if _lock_path.exists():
            _old_pid = int(_lock_path.read_text().strip())
            if _old_pid != _my_pid:
                log.warning("lock_file_found_killing_old_instance", old_pid=_old_pid)
                try:
                    if sys.platform == "win32":
                        os.system(f"taskkill /F /PID {_old_pid} >nul 2>&1")
                    else:
                        os.kill(_old_pid, 15)  # SIGTERM
                    await asyncio.sleep(2.0)
                except Exception:
                    pass
        _lock_path.write_text(str(_my_pid))
        log.info("lock_file_written", pid=_my_pid)
    except Exception as _lock_exc:
        log.warning("lock_file_failed", error=str(_lock_exc))

    kalshi        = KalshiClient()
    claude        = ClaudeClient()
    db            = DBLogger()
    order_mgr     = OrderManager(kalshi=kalshi, db=db)
    tracker       = PerformanceTracker()
    period_stats  = _PeriodStats()
    intel_scanner = IntelligenceScanner(kalshi)
    ta_analyzer   = TechnicalAnalyzer()
    news_intel    = NewsIntelligence(kalshi)
    # IMAP: prefer KAL_EMAIL_ADDRESS/PASSWORD (Outlook/any provider),
    # fall back to old KAL_GMAIL_ADDRESS/APP_PASSWORD for existing setups
    _imap_addr = (
        getattr(settings, "kal_email_address", "")
        or getattr(settings, "kal_gmail_address", "")
    )
    _imap_pass = (
        getattr(settings, "kal_email_password", "")
        or getattr(settings, "kal_gmail_app_password", "")
    )
    gmail_reader  = GmailReader(
        credentials_path=getattr(settings, "gmail_credentials_path", "./gmail_credentials.json"),
        token_path=getattr(settings, "gmail_token_path", "./gmail_token.json"),
        imap_address=_imap_addr,
        imap_password=_imap_pass,
    )
    # Callable that returns the override model when daily limit is hit
    def _model_override_fn() -> str | None:
        return settings.claude_fallback_model if _use_fallback_model else None

    await discord.notify_bot_started(mode=mode, demo=settings.demo_mode)
    await discord.send_channel_guide()
    journal_mod.log_session_start(mode=mode, demo=settings.demo_mode)

    try:
        intel_interval = getattr(settings, "intelligence_scan_interval", 30)

        if settings.paper_trading:
            # Paper trading: live scan loop on demo API, resolve every 5 min
            log.info("paper_trading_mode_active", scan_interval=settings.scan_interval_minutes)
            resolution_task  = asyncio.create_task(
                _resolution_checker_task(tracker, kalshi, interval_minutes=5, period_stats=period_stats)
            )
            summary_task     = asyncio.create_task(
                _periodic_summary_task(period_stats, tracker, interval_hours=6)
            )
            performance_task = asyncio.create_task(
                _daily_performance_task(tracker, kalshi, interval_hours=24)
            )
            intelligence_task = asyncio.create_task(
                _intelligence_scan_task(intel_scanner, interval_minutes=intel_interval)
            )
            ta_refresh_task   = asyncio.create_task(
                _ta_refresh_task(ta_analyzer, interval_minutes=15)
            )
            ta_summary_task   = asyncio.create_task(
                _ta_summary_task(ta_analyzer, interval_hours=1)
            )
            news_task         = asyncio.create_task(
                _news_intelligence_task(news_intel, kalshi, interval_minutes=30)
            )
            scheduled_task    = asyncio.create_task(
                _scheduled_analysis_task(ta_analyzer, tracker, kalshi, model_override_fn=_model_override_fn)
            )
            gmail_task        = asyncio.create_task(
                _gmail_brief_task(gmail_reader, kalshi, model_override_fn=_model_override_fn)
            )
            axios_alerts_task = asyncio.create_task(
                _axios_alerts_task(gmail_reader, kalshi, model_override_fn=_model_override_fn)
            )
            calendar_task     = asyncio.create_task(
                _economic_calendar_task(kalshi)
            )
            market_open_task  = asyncio.create_task(_market_open_task())
            market_close_task = asyncio.create_task(
                _market_close_task(model_override_fn=_model_override_fn)
            )
            credit_check_task = asyncio.create_task(_credit_check_task())

            async def paper_cycle() -> None:
                return await run_live_scan(kalshi, claude, db, order_mgr, tracker, period_stats)

            await paper_cycle()
            while True:
                if _credits_exhausted:
                    log.info("credits_exhausted_pausing", secs=_CREDIT_PAUSE_SECS)
                    await asyncio.sleep(_CREDIT_PAUSE_SECS)
                else:
                    sleep_secs = _seconds_until_next_scan()
                    log.info("window_sleep", seconds=round(sleep_secs), next_window=_current_window_close)
                    await asyncio.sleep(sleep_secs)
                await paper_cycle()

        elif settings.research_mode:
            intelligence_task = asyncio.create_task(
                _intelligence_scan_task(intel_scanner, interval_minutes=intel_interval)
            )
            asyncio.create_task(_ta_refresh_task(ta_analyzer, interval_minutes=15))
            await run_research_scan(kalshi, claude, db, tracker)
            log.info("research_complete_awaiting_funding")
            # Send performance report at end of research run
            try:
                today_stats   = tracker.get_stats(today_only=True)
                alltime_stats = tracker.get_stats(today_only=False)
                if alltime_stats["total_trades"] > 0:
                    await discord.notify_daily_performance(
                        today_stats=today_stats,
                        all_time_stats=alltime_stats,
                        xlsx_bytes=tracker.get_xlsx_bytes(),
                        demo=settings.demo_mode,
                        resolved_count=0,
                    )
            except Exception as exc:
                log.warning("post_research_performance_send_failed", error=str(exc))
        else:
            # Launch background tasks
            summary_task      = asyncio.create_task(
                _periodic_summary_task(period_stats, tracker, interval_hours=6)
            )
            performance_task  = asyncio.create_task(
                _daily_performance_task(tracker, kalshi, interval_hours=24)
            )
            intelligence_task = asyncio.create_task(
                _intelligence_scan_task(intel_scanner, interval_minutes=intel_interval)
            )
            ta_refresh_task   = asyncio.create_task(
                _ta_refresh_task(ta_analyzer, interval_minutes=15)
            )
            ta_summary_task   = asyncio.create_task(
                _ta_summary_task(ta_analyzer, interval_hours=1)
            )
            news_task         = asyncio.create_task(
                _news_intelligence_task(news_intel, kalshi, interval_minutes=30)
            )
            scheduled_task    = asyncio.create_task(
                _scheduled_analysis_task(ta_analyzer, tracker, kalshi, model_override_fn=_model_override_fn)
            )
            gmail_task        = asyncio.create_task(
                _gmail_brief_task(gmail_reader, kalshi, model_override_fn=_model_override_fn)
            )
            axios_alerts_task = asyncio.create_task(
                _axios_alerts_task(gmail_reader, kalshi, model_override_fn=_model_override_fn)
            )
            calendar_task     = asyncio.create_task(
                _economic_calendar_task(kalshi)
            )
            market_open_task  = asyncio.create_task(_market_open_task())
            market_close_task = asyncio.create_task(
                _market_close_task(model_override_fn=_model_override_fn)
            )
            credit_check_task = asyncio.create_task(_credit_check_task())

            async def live_cycle() -> None:
                return await run_live_scan(kalshi, claude, db, order_mgr, tracker, period_stats)

            # Run immediately, then sleep until current window closes
            await live_cycle()

            while True:
                if _credits_exhausted:
                    log.info("credits_exhausted_pausing", secs=_CREDIT_PAUSE_SECS)
                    await asyncio.sleep(_CREDIT_PAUSE_SECS)
                else:
                    sleep_secs = _seconds_until_next_scan()
                    log.info("window_sleep", seconds=round(sleep_secs))
                    await asyncio.sleep(sleep_secs)
                await live_cycle()

    except Exception as exc:
        import traceback
        log.error("fatal_error", error=str(exc), traceback=traceback.format_exc())
        await _report_error(exc, "Fatal error in main loop", critical=True)
        raise
    finally:
        # Release lock file
        try:
            if _lock_path.exists() and int(_lock_path.read_text().strip()) == _my_pid:
                _lock_path.unlink()
                log.info("lock_file_released")
        except Exception:
            pass
        await kalshi.close()
        await discord.notify_bot_stopped()


def main() -> None:
    # Parse --research / --live / --paper flags to override env setting
    if "--paper" in sys.argv:
        settings.paper_trading  = True
        settings.research_mode  = False
        settings.demo_mode      = True
    elif "--research" in sys.argv:
        settings.research_mode  = True
        settings.paper_trading  = False
    elif "--live" in sys.argv:
        settings.research_mode  = False
        settings.paper_trading  = False

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
