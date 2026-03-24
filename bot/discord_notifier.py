"""
discord_notifier.py — Discord notifications for Kal.

15 channels across 5 categories:
  🧠 INTELLIGENCE  morning-brief, breaking-news, big-money, thesis
  📊 MARKETS       trades, watchlist, weekly-analysis
  📈 ASSET CLASSES crypto, stocks, prediction-markets, commodities
  🚨 SIGNALS       high-conviction, intelligence-feed
  ⚙️ SYSTEM        summary, alerts

All messages:
  - Plain English — full coin names (Bitcoin, Ethereum, Solana)
  - Bold headers with ** **
  - Eastern Time for all timestamps
  - No raw ticker strings in human-facing text
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

KAL = "Kal"

# Module-level bot instance — set by send_channel_guide() after startup.
# When set, _send() routes through the bot API (channel IDs) instead of webhooks.
_bot: "DiscordBot | None" = None

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_GREEN  = 0x00C076
COLOR_RED    = 0xEF4444
COLOR_BLUE   = 0x3B82F6
COLOR_GOLD   = 0xF1C40F
COLOR_ORANGE = 0xE67E22
COLOR_GRAY   = 0x374151

# ── Coin name mapping ─────────────────────────────────────────────────────────
COIN_FULL = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
}

# ── Time helpers ──────────────────────────────────────────────────────────────

def _utc_to_et(dt: datetime.datetime) -> str:
    """Convert UTC datetime to Eastern Time (handles EDT/EST automatically)."""
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        et = dt.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    except (ImportError, KeyError):
        # Windows without tzdata package: ZoneInfoNotFoundError is a subclass of KeyError
        # Fallback: EDT = UTC-4 (valid roughly March–November)
        et = dt.replace(tzinfo=None) + datetime.timedelta(hours=-4)
    h = et.hour % 12 or 12
    ampm = "am" if et.hour < 12 else "pm"
    return f"{h}:{et.minute:02d}{ampm} ET"


def _now_et() -> str:
    return _utc_to_et(datetime.datetime.utcnow())


def _now_utc_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


# ── Webhook routing ───────────────────────────────────────────────────────────

def _get_webhook(channel: str) -> str | None:
    """
    Return the webhook URL for a channel, falling back to the legacy url.
    channel: any channel key, e.g. "trades" | "crypto" | "intelligence-feed" | "alerts"
    """
    specific = getattr(settings, f"discord_webhook_{channel}", "")
    if specific:
        return specific
    # Fall back to the legacy single webhook
    fallback = getattr(settings, "discord_webhook_url", "")
    return fallback or None


async def _send(channel: str, payload: dict) -> None:
    """
    Fire-and-forget send. Never raises — Discord must never block the bot.

    Routing priority:
      1. Bot API (direct channel send) — used when send_channel_guide() has run
      2. Channel-specific webhook URL  — fallback if bot not set up
      3. Legacy single webhook URL     — last resort
    """
    # ── 1. Bot API ────────────────────────────────────────────────────────────
    if _bot is not None:
        ch_id = _bot.channel_id(channel)
        if ch_id:
            try:
                await _bot.send(ch_id, payload)
                return
            except Exception as exc:
                log.warning("[discord/%s] bot send failed, falling back to webhook: %s", channel, exc)

    # ── 2 & 3. Webhook fallback ───────────────────────────────────────────────
    url = _get_webhook(channel)
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code not in (200, 204):
                log.warning("[discord/%s] %s %s", channel, resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("[discord/%s] send failed: %s", channel, exc)


async def _send_file(channel: str, payload: dict, filename: str, file_bytes: bytes, mime: str) -> None:
    """Send a message with a file attachment."""
    url = _get_webhook(channel)
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                data={"payload_json": json.dumps(payload)},
                files={"file": (filename, file_bytes, mime)},
            )
            if resp.status_code not in (200, 204):
                log.warning("[discord/%s] file send %s %s", channel, resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("[discord/%s] file send failed: %s", channel, exc)


def _embed(
    description: str,
    color: int,
    title: str = "",
    fields: list[dict] | None = None,
    footer: str | None = None,
) -> dict:
    e: dict[str, Any] = {
        "description": description,
        "color": color,
        "timestamp": _now_utc_iso(),
    }
    if title:
        e["title"] = title
    if fields:
        e["fields"] = fields
    if footer:
        e["footer"] = {"text": footer}
    return {"embeds": [e], "username": KAL}


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

_ONLINE_COOLDOWN_SECS = 600  # 10 minutes between "Kal is online" posts
_ONLINE_STAMP_PATH   = Path(__file__).parent / "kal_last_online.txt"


async def notify_bot_started(mode: str, demo: bool) -> None:
    # Cooldown: only post once per 10 minutes to prevent restart spam
    try:
        if _ONLINE_STAMP_PATH.exists():
            last_ts = float(_ONLINE_STAMP_PATH.read_text().strip())
            if time.time() - last_ts < _ONLINE_COOLDOWN_SECS:
                log.info("bot_started_cooldown_skip")
                return
    except Exception:
        pass
    try:
        _ONLINE_STAMP_PATH.write_text(str(time.time()))
    except Exception:
        pass

    env = "paper" if demo else "live capital"
    mode_str = {"paper": "Paper trading", "research": "Research", "live": "Live trading"}.get(mode, mode.capitalize())
    await _send("alerts", _embed(
        f"**Kal is online.** {mode_str} mode — {env}.\nWatching BTC, ETH, SOL 15-minute markets.",
        COLOR_BLUE,
        footer=_now_et(),
    ))


async def notify_bot_stopped(reason: str = "clean shutdown") -> None:
    await _send("alerts", _embed(
        f"**Kal is offline.** {reason}",
        COLOR_GRAY,
        footer=_now_et(),
    ))


# ── Analysis ──────────────────────────────────────────────────────────────────

async def notify_ai_decision(
    ticker: str,
    title: str,
    recommendation: str,
    edge: float,
    confidence: float,
    kalshi_price: float,
    claude_prob: float,
    reasoning: str,
    mode: str = "paper",
    expected_profit: float = 0.0,
    tradeable_score: float = 0.0,
    volume: float = 0.0,
    coin: str = "",
    timeframe: str = "",
    live_price: float = 0.0,
    strike_price: float = 0.0,
    direction: str = "",
    change_24h: float = 0.0,
) -> None:
    """Post a market analysis to #analysis. Filters out low-quality noise."""
    # SKIP decisions never post
    if recommendation == "SKIP":
        return

    # Issue 3: only post if the market is worth paying attention to.
    # Must meet at least one: volume > $50, confidence > 50%, edge > 15%.
    if not (volume > 50 or confidence > 0.50 or abs(edge) > 0.15):
        return

    coin_full = COIN_FULL.get(coin.upper(), coin or ticker)
    is_yes = recommendation == "BUY_YES"

    # Direction language
    if direction:
        dir_word = "UP" if direction.upper() == "ABOVE" else "DOWN"
    else:
        dir_word = "UP" if is_yes else "DOWN"

    # Window time
    close_str = ""
    if title and "15 min" in title.lower():
        close_str = "15-minute window"
    elif timeframe:
        close_str = f"{timeframe} window"

    crowd_pct = round(kalshi_price * 100)
    kal_pct   = round(claude_prob * 100)
    edge_pct  = round(abs(edge) * 100)
    conf_pct  = round(confidence * 100)

    vol_str = f"${volume:,.0f}" if volume > 0 else "$0 — no volume"
    low_vol = volume < 200

    lines = [
        f"**Watching — {coin_full}** {close_str}",
        f"Kal sees the crowd pricing {coin_full} {'UP' if kalshi_price >= 0.5 else 'DOWN'} at **{crowd_pct}%** — he thinks that's {'too high' if kalshi_price >= 0.5 else 'too low'}.",
        f"His read: closer to **{kal_pct}%**. That's a **{edge_pct}% edge**.",
        f"Confidence: {conf_pct}% | Volume: {vol_str}",
    ]
    if low_vol:
        lines.append("Low volume — watching only, not sizing up")

    reason_short = reasoning.split("|")[0].strip()[:200]
    if reason_short:
        lines.append(f"\n*\"{reason_short}\"*")

    # Issue 5: plain English footer
    if is_yes:
        plain = (
            f"In plain English: The crowd gives this a {crowd_pct}% chance. "
            f"Kal thinks it's closer to {kal_pct}% — the crowd is underpricing it."
        )
    else:
        plain = (
            f"In plain English: The crowd gives this a {crowd_pct}% chance. "
            f"Kal thinks it's closer to {kal_pct}% — the crowd is overconfident."
        )
    lines.append(f"\n{plain}")

    mode_tag = "paper" if mode in ("paper", "research") else "live"
    await _send("crypto", _embed(
        "\n".join(lines),
        COLOR_GREEN if is_yes else COLOR_RED,
        footer=f"{coin_full} · {mode_tag} · {_now_et()}",
    ))


# ── Trade placed ──────────────────────────────────────────────────────────────

async def notify_trade_placed(
    ticker: str,
    market_title: str,
    side: str,
    contracts: int,
    price_cents: int,
    amount_dollars: float,
    order_id: str,
    demo: bool,
    # Optional enrichment — pass from analysis context when available
    coin: str = "",
    live_price: float = 0.0,
    strike_price: float = 0.0,
    close_time: str = "",
    volume: float = 0.0,
) -> None:
    # Infer coin from ticker if not passed
    if not coin:
        for c in ("BTC", "ETH", "SOL"):
            if c in ticker.upper():
                coin = c
                break

    coin_full  = COIN_FULL.get(coin.upper(), coin or "Crypto")
    is_yes     = side.lower() == "yes"
    direction  = "UP" if is_yes else "DOWN"
    side_label = "YES (betting price goes up)" if is_yes else "NO (betting price goes down)"
    win_cond   = f"{coin_full} price must be **HIGHER** at resolution" if is_yes else f"{coin_full} price must be **LOWER** at resolution"

    # Resolve time in ET
    resolve_str = ""
    if close_time:
        try:
            ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            resolve_str = f" at {_utc_to_et(ct)}"
        except Exception:
            pass

    # Price display
    odds_str = f"{price_cents}¢"
    price_line = ""
    if live_price > 0 and strike_price > 0:
        price_line = f"Current {coin_full} price: **${live_price:,.2f}** | Strike: **${strike_price:,.2f}**\n"
    elif live_price > 0:
        price_line = f"Current {coin_full} price: **${live_price:,.2f}**\n"

    # Volume warning
    vol_warn = ""
    if volume > 0 and volume < 200:
        vol_warn = f"\n⚠️ Low volume (${volume:,.0f}) — paper only"
    elif volume == 0:
        vol_warn = "\n⚠️ Zero volume — paper only"

    tag = "paper" if demo else "live"

    lines = [
        f"**Trade Placed** — {tag}",
        f"Kal is betting {coin_full} goes **{direction}** in the next 15 minutes.",
        f"For this trade to win: {win_cond}{resolve_str}.",
        price_line.strip() if price_line else "",
        f"Direction: {side_label}",
        f"Amount: **${amount_dollars:.2f}** | Contracts: **{contracts}** | Odds: {odds_str}",
    ]
    if vol_warn:
        lines.append(vol_warn)

    description = "\n".join(l for l in lines if l)

    await _send("trades", _embed(
        description,
        COLOR_GREEN if is_yes else COLOR_BLUE,
        footer=f"{ticker} · {tag} · {_now_et()}",
    ))


# ── Trade resolved ────────────────────────────────────────────────────────────

async def notify_trade_resolved(
    ticker: str,
    market_title: str,
    won: bool,
    pnl: float,
    running_pnl: float,
    wins: int,
    losses: int,
    balance: float,
    mode: str = "live",
) -> None:
    # Infer coin from ticker
    coin = ""
    for c in ("BTC", "ETH", "SOL"):
        if c in ticker.upper():
            coin = c
            break
    coin_full = COIN_FULL.get(coin, coin or "The market")

    # Infer what happened
    is_yes_bet = "_YES" in ticker.upper() or (market_title and "up" in market_title.lower())
    # Better: check the direction from ticker pattern or market title
    # "SOL price up in next 15 mins?" → BUY NO means we bet it goes down

    total = wins + losses
    record = f"{wins}-{losses}"
    win_rate = f"{wins/total*100:.0f}%" if total else "—"

    if won:
        pnl_line = f"+${pnl:.2f} profit | Record: {record} | Balance: ${balance:,.2f}"
        main = f"**Win** — {coin_full} went the right way. Called it."
        color = COLOR_GREEN
    else:
        pnl_line = f"-${abs(pnl):.2f} | Record: {record} | Balance: ${balance:,.2f}"
        main = f"**Loss** — {coin_full} didn't cooperate. Happens."
        color = COLOR_RED

    tag = "paper" if mode.upper() not in ("LIVE",) else "live"
    await _send("trades", _embed(
        f"{main}\n{pnl_line}",
        color,
        footer=f"{ticker} · {tag} · win rate {win_rate} · {_now_et()}",
    ))


# ── Position update ───────────────────────────────────────────────────────────

async def notify_position_update(pending_trades: list[dict]) -> None:
    """
    Called after any trade resolves. Shows all currently open positions.
    pending_trades: list of CSV row dicts with keys like Coin, _side, Market Title, _ticker
    """
    if not pending_trades:
        await _send("trades", _embed(
            "**Position Update**\nNo open positions. Flat.",
            COLOR_GRAY,
            footer=_now_et(),
        ))
        return

    lines = [f"**Position Update** — {len(pending_trades)} open trade{'s' if len(pending_trades) != 1 else ''}"]
    for row in pending_trades:
        coin = row.get("Coin", "")
        coin_full = COIN_FULL.get(coin.upper(), coin or "?")
        side = row.get("_side", "").lower()
        direction = "UP" if side == "yes" else "DOWN"
        amount = row.get("Amount Wagered ($)", "")
        try:
            amt_str = f"${float(amount):.2f}"
        except (ValueError, TypeError):
            amt_str = ""
        line = f"- {coin_full} **{direction}** — pending resolution"
        if amt_str:
            line += f" | {amt_str}"
        lines.append(line)

    await _send("trades", _embed(
        "\n".join(lines),
        COLOR_BLUE,
        footer=_now_et(),
    ))


# ── Trade failed ──────────────────────────────────────────────────────────────

async def notify_trade_failed(
    ticker: str,
    market_title: str,
    error: str,
) -> None:
    coin = next((c for c in ("BTC", "ETH", "SOL") if c in ticker.upper()), "")
    coin_full = COIN_FULL.get(coin, ticker)
    await _send("alerts", _embed(
        f"**Order Rejected** — {coin_full}\nNo capital deployed.\n```{error[:200]}```",
        COLOR_ORANGE,
        footer=_now_et(),
    ))


# ── Research complete ─────────────────────────────────────────────────────────

async def notify_research_complete(
    markets_scanned: int,
    markets_analyzed: int,
    overall_edge: float,
    top_categories: list[dict],
    recommended_funding: float,
    duration_minutes: float,
    liquid_actionable: int = 0,
    zero_volume_flagged: int = 0,
    overall_score: float = 0.0,
) -> None:
    cat_names = [c.get("category", "?") for c in top_categories[:3]]
    cats_str  = " + ".join(cat_names) if cat_names else "none found"
    top_call  = ""
    if top_categories:
        t = top_categories[0]
        top_call = (
            f"{t.get('category','?')} — "
            f"{t.get('edge_avg',0):.1%} avg edge, "
            f"{t.get('sample_size',0)} markets"
        )

    lines = [
        f"**Research Complete** — scanned {markets_analyzed} markets in {duration_minutes:.0f} min.",
        f"Best edge today: {cats_str}",
        f"Top category: {top_call}",
        f"Avg edge: {overall_edge:.1%} | Liquid & actionable: {liquid_actionable} | Funding rec: ${recommended_funding:,.0f}",
        f"Ready when you are.",
    ]
    await _send("summary", _embed(
        "\n".join(lines),
        COLOR_GOLD,
        footer=f"{markets_scanned} markets scanned · {_now_et()}",
    ))


# ── Periodic summary ──────────────────────────────────────────────────────────

async def notify_periodic_summary(
    period_hours: int,
    markets_scanned: int,
    ai_calls: int,
    trades_placed: int,
    top_opportunities: list[dict],
    demo: bool,
    balance: float = 500.0,
    running_pnl: float = 0.0,
    total_wins: int = 0,
    total_losses: int = 0,
) -> None:
    tag = " (paper)" if demo else ""
    now = _now_et()

    # Header
    lines = [f"**Check In — {now}{tag}**"]

    # Activity line
    activity_parts = []
    if markets_scanned:
        activity_parts.append(f"{markets_scanned} markets scanned")
    if ai_calls:
        activity_parts.append(f"{ai_calls} AI calls")
    if trades_placed:
        activity_parts.append(f"{trades_placed} trade{'s' if trades_placed != 1 else ''} placed")
    if activity_parts:
        lines.append(" · ".join(activity_parts))

    # Record and balance
    total_trades = total_wins + total_losses
    if total_trades > 0:
        win_rate = total_wins / total_trades
        wr_str = f"{win_rate:.0%}"
    else:
        wr_str = "—"
    pnl_str = f"+${running_pnl:.2f}" if running_pnl >= 0 else f"-${abs(running_pnl):.2f}"
    lines.append(f"Record: {total_wins}W / {total_losses}L ({wr_str}) · P&L: {pnl_str} · Balance: ${balance:.2f}")

    # Best edge
    if top_opportunities:
        best = top_opportunities[0]
        edge = best.get("edge", 0)
        coin = next((c for c in ("BTC", "ETH", "SOL") if c in best.get("ticker", "").upper()), "")
        coin_full = COIN_FULL.get(coin, best.get("ticker", "?"))
        ttl = best.get("title", "")[:55]
        lines.append(f"Best edge: {coin_full} {edge:+.1%} — {ttl}")

    color = COLOR_GREEN if running_pnl > 0 else COLOR_RED if running_pnl < 0 else COLOR_BLUE
    await _send("summary", _embed(
        "\n".join(lines),
        color,
        footer=f"next update in {period_hours}h",
    ))


# ── Credit alert ──────────────────────────────────────────────────────────────

async def notify_credit_alert(
    markets_scanned: int,
    period_hours: int,
) -> None:
    """Posted to #alerts when markets are scanned but Claude is never called (API credit issue)."""
    lines = [
        "**Credit Alert**",
        f"Kal scanned {markets_scanned} markets in the last {period_hours}h but made 0 AI calls.",
        "This usually means Claude API credits are exhausted or the key is invalid.",
        "Check your Anthropic account and restart the bot after topping up.",
    ]
    await _send("alerts", _embed(
        "\n".join(lines),
        COLOR_RED,
        footer=_now_et(),
    ))


# ── Daily summary ─────────────────────────────────────────────────────────────

async def notify_daily_summary(
    date: str,
    markets_scanned: int,
    trades_placed: int,
    trades_filled: int,
    gross_pnl: float,
    net_pnl: float,
    win_rate: float | None,
    demo: bool,
) -> None:
    wr_str = f"{win_rate:.0%}" if win_rate is not None else "—"
    tag    = " (paper)" if demo else ""

    if net_pnl > 0:
        main  = f"**Green day{tag}.** +${net_pnl:.2f} net."
        color = COLOR_GREEN
    elif net_pnl < 0:
        main  = f"**Down ${abs(net_pnl):.2f} today{tag}.** Edge is still there."
        color = COLOR_RED
    else:
        main  = f"**Flat on the day{tag}.** Capital preserved."
        color = COLOR_BLUE

    detail = f"{trades_placed} trades placed | Win rate: {wr_str} | Gross: ${gross_pnl:+.2f}"
    await _send("summary", _embed(
        f"{main}\n{detail}",
        color,
        footer=f"{date} · {_now_et()}",
    ))


# ── Daily performance report (with XLSX) ─────────────────────────────────────

async def notify_daily_performance(
    today_stats:    dict,
    all_time_stats: dict,
    xlsx_bytes:     bytes,
    demo:           bool,
    resolved_count: int = 0,
) -> None:
    today    = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    tag      = " (paper)" if demo else ""
    total_pnl = today_stats.get("total_pnl", 0.0)
    wins      = today_stats.get("wins", 0)
    losses    = today_stats.get("losses", 0)
    win_rate  = today_stats.get("win_rate", 0.0)
    at_pnl    = all_time_stats.get("total_pnl", 0.0)
    at_trades = all_time_stats.get("total_trades", 0)
    at_wr     = all_time_stats.get("win_rate", 0.0)
    best      = all_time_stats.get("best_trade",  (0.0, "—", ""))
    worst     = all_time_stats.get("worst_trade", (0.0, "—", ""))

    if total_pnl > 0:
        color = COLOR_GREEN
        main  = f"**Green day{tag}.** +${total_pnl:.2f}. Full report attached."
    elif total_pnl < 0:
        color = COLOR_RED
        main  = f"**Down ${abs(total_pnl):.2f} today{tag}.** Full breakdown in the file."
    else:
        color = COLOR_BLUE
        main  = f"**Daily report{tag}.** Full trade log attached."

    description = (
        f"{main}\n"
        f"Today: {wins}W / {losses}L | Win rate: {win_rate:.1f}% | P&L: ${total_pnl:+.2f}\n"
        f"All-time: {at_trades} trades | {at_wr:.1f}% win rate | ${at_pnl:+.2f}"
    )
    embed: dict = {
        "description": description,
        "color": color,
        "timestamp": _now_utc_iso(),
        "footer": {"text": f"{today} · {resolved_count} resolved · {_now_et()}"},
        "fields": [
            {"name": "Best trade",  "value": f"${best[0]:+.2f} — {best[1][:45]}",  "inline": False},
            {"name": "Worst trade", "value": f"${worst[0]:+.2f} — {worst[1][:45]}", "inline": False},
        ],
    }
    payload = {"embeds": [embed], "username": KAL}
    await _send_file(
        "summary", payload,
        "kal_performance.xlsx", xlsx_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Errors / alerts ───────────────────────────────────────────────────────────

async def notify_credits_exhausted() -> None:
    """Posted ONCE when Anthropic credits run out. Silenced until restored."""
    await _send("alerts", _embed(
        "**Out of Credits**\n"
        "Kal has run out of Anthropic API credits and cannot analyze markets.\n"
        "Add credits at console.anthropic.com to resume.\n"
        "Kal will notify you when analysis resumes.",
        COLOR_RED,
        footer=_now_et(),
    ))


async def notify_credits_restored() -> None:
    """Posted once when credits come back and the first API call succeeds."""
    await _send("alerts", _embed(
        "**Back Online**\n"
        "Credits restored. Kal is analyzing markets again.",
        COLOR_GREEN,
        footer=_now_et(),
    ))


async def notify_error(
    context: str,
    error: str,
    critical: bool = False,
) -> None:
    # Strip any traceback that slipped through
    if "Traceback" in error:
        error = error[:error.index("Traceback")].strip()
    error = error[:300]

    if critical:
        lines = [
            "**Critical Error — Kal stopped**",
            f"{context}",
            f"{error}",
        ]
        color = COLOR_RED
    else:
        lines = [
            "**Something went wrong**",
            f"{context}: {error}" if context else error,
            "Kal is still running. Monitoring for resolution.",
        ]
        color = COLOR_ORANGE

    await _send("alerts", _embed("\n".join(lines), color, footer=_now_et()))


async def notify_cost_warning(
    spent: float,
    warning_limit: float,
    hard_limit: float,
    weekly_pace: float,
    trades_placed: int,
    wins: int,
    losses: int,
) -> None:
    """$1.00 threshold warning — approaching daily limit."""
    await _send("alerts", _embed(
        f"**Cost Check**\n"
        f"Spent **${spent:.2f}** today on analysis. Approaching daily limit of **${hard_limit:.2f}**.\n"
        f"Trades placed: {trades_placed} | Wins: {wins} | Losses: {losses}\n"
        f"On pace to spend **${weekly_pace:.2f}** this week.",
        COLOR_ORANGE,
        footer=_now_et(),
    ))


async def notify_cost_limit_hit(
    spent: float,
    hard_limit: float,
    fallback_model: str,
) -> None:
    """$1.50 hard limit hit — switched to economy model."""
    short_name = fallback_model.split("-")[1].capitalize() if "-" in fallback_model else fallback_model
    await _send("alerts", _embed(
        f"**Cost Limit Hit**\n"
        f"Spent **${spent:.2f}** today — daily limit of **${hard_limit:.2f}** reached.\n"
        f"Switched to economy mode ({short_name}) for the rest of today.\n"
        f"Will reset at midnight ET.",
        COLOR_RED,
        footer=_now_et(),
    ))


async def notify_daily_cost_report(
    date: str,
    total_cost: float,
    total_calls: int,
    trades_placed: int,
    model_primary: str,
    model_fallback: str,
    fallback_calls: int,
) -> None:
    """End-of-day cost summary posted to #summary."""
    avg_cost = total_cost / total_calls if total_calls else 0.0
    weekly_pace = total_cost * 7
    primary_calls = total_calls - fallback_calls
    lines = [
        f"**Daily Cost Report** — {date}",
        f"Total spent: **${total_cost:.4f}** across **{total_calls}** Claude calls",
        f"Avg per call: ${avg_cost:.4f} | Weekly pace: **${weekly_pace:.2f}**",
        f"Primary model ({model_primary.split('-')[1] if '-' in model_primary else model_primary}): {primary_calls} calls",
    ]
    if fallback_calls:
        lines.append(f"Economy model ({model_fallback.split('-')[1] if '-' in model_fallback else model_fallback}): {fallback_calls} calls")
    lines.append(f"Trades placed from {total_calls} analyses: {trades_placed}")
    await _send("summary", _embed(
        "\n".join(lines),
        COLOR_BLUE if total_cost < 1.00 else COLOR_ORANGE,
        footer=f"cost tracking · {_now_et()}",
    ))


async def notify_connection_issue(service: str, error: str) -> None:
    await _send("alerts", _embed(
        f"**Lost connection to {service}.** Not placing orders until restored.\n`{error[:200]}`",
        COLOR_ORANGE,
        footer=_now_et(),
    ))


# ── Technical Analysis notifications ──────────────────────────────────────────

async def notify_ta_hourly_summary(coin: str, summary_text: str) -> None:
    """Post hourly TA summary to #crypto."""
    coin_full = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}.get(coin.upper(), coin)
    await _send("crypto", _embed(
        summary_text,
        COLOR_BLUE,
        footer=f"{coin_full} · technical · {_now_et()}",
    ))


async def notify_market_pulse(pulse_text: str) -> None:
    """Post the combined hourly Market Pulse (BTC+ETH+SOL) to #crypto."""
    await _send("crypto", {
        "content": pulse_text,
        "username": KAL,
    })


# ── News Intelligence notifications ───────────────────────────────────────────

async def notify_news_thesis(thesis: str, markets: list[dict], headline: str) -> None:
    """Post a full news-to-market thesis to #intelligence-feed."""
    await _send("intelligence-feed", {
        "embeds": [{
            "title": "Thesis",
            "description": thesis[:3900],
            "color": COLOR_GOLD,
            "timestamp": _now_utc_iso(),
            "footer": {"text": f"news · {_now_et()}"},
        }],
        "username": KAL,
    })


async def notify_breaking_news(article: dict, markets: list[dict]) -> None:
    """Immediately post breaking news + matched markets to #breaking-news."""
    headline = article.get("headline", "Breaking news")[:200]
    summary  = (article.get("summary") or "")[:300]
    source   = article.get("source", "")

    lines = [f"**Breaking — {source}**", headline]
    if summary and summary.lower() != headline.lower()[:len(summary)]:
        lines.append(f"_{summary}_")
    if markets:
        lines.append("")
        lines.append("**Related Kalshi markets:**")
        for m in markets[:3]:
            pct = round(m.get("yes_price", 0.5) * 100)
            lines.append(f"• {m['title'][:60]} — {pct}%")

    await _send("breaking-news", _embed(
        "\n".join(lines),
        COLOR_RED,
        footer=f"breaking · {_now_et()}",
    ))


async def notify_economic_calendar(events: list[dict], markets: list[dict]) -> None:
    """Post today's economic calendar to #intelligence at 8am ET."""
    today = datetime.datetime.utcnow().strftime("%A, %B %-d")

    # Organise events by impact
    high    = [e for e in events if int(e.get("impact", 0) or 0) >= 3]
    medium  = [e for e in events if int(e.get("impact", 0) or 0) == 2]
    display = (high + medium)[:10]

    lines = [f"**Market Calendar — {today}**", ""]
    if not display:
        lines.append("No high-impact events scheduled today.")
    else:
        lines.append("**High-impact events today:**")
        for e in display:
            t    = e.get("time", "")[:5]
            name = (e.get("event") or e.get("name") or "")[:60]
            imp  = int(e.get("impact", 2) or 2)
            star = "🔴" if imp >= 3 else "🟡"
            lines.append(f"• {star} {t} ET — {name}")

    # Surface any Kalshi markets matching economic events
    if markets and display:
        from news_intelligence import extract_topics, score_market_relevance
        event_text = " ".join(e.get("event", "") or "" for e in display[:5])
        topics = extract_topics(event_text)
        if topics:
            matched = []
            for m in markets:
                title = m.get("title") or m.get("subtitle") or ""
                score = score_market_relevance(title, topics)
                if score >= 0.4:
                    vol_fp = m.get("volume_fp")
                    volume = float(vol_fp) / 100.0 if vol_fp is not None else 0.0
                    yes_ask = m.get("yes_ask_dollars")
                    pct = round(float(yes_ask) * 100) if yes_ask else 50
                    matched.append((score, title, pct, volume))
            matched.sort(reverse=True)
            if matched:
                lines += ["", "**Related Kalshi markets to watch:**"]
                for _, title, pct, vol in matched[:3]:
                    lines.append(f"• {title[:60]} — {pct}%")

    await _send("intelligence-feed", _embed(
        "\n".join(lines),
        COLOR_BLUE,
        footer=f"calendar · {_now_et()}",
    ))


# ── Scheduled analysis notifications ──────────────────────────────────────────

async def notify_daily_briefing(content: str) -> None:
    """Post the market-data morning briefing to #morning-brief."""
    await _send("morning-brief", {"content": content, "username": KAL})


async def notify_weekly_analysis(content: str) -> None:
    """Post the weekly analysis to #weekly-analysis."""
    await _send("weekly-analysis", {"content": content, "username": KAL})


async def notify_morning_brief(brief: str) -> None:
    """Post the Gmail newsletter morning brief to #morning-brief."""
    # Discord messages cap at 2000 chars — split if needed
    if len(brief) <= 1990:
        await _send("morning-brief", {"content": brief, "username": KAL})
    else:
        # Split at a paragraph boundary near the halfway point
        mid   = len(brief) // 2
        split = brief.rfind("\n\n", 0, mid + 300)
        if split == -1:
            split = mid
        part1 = brief[:split].strip()
        part2 = brief[split:].strip()
        await _send("morning-brief", {"content": part1, "username": KAL})
        await asyncio.sleep(0.5)
        await _send("morning-brief", {"content": part2, "username": KAL})


async def notify_todays_focus(focus: str) -> None:
    """Cross-post the TODAY'S FOCUS section from the brief to #intelligence-feed."""
    if not focus:
        return
    await _send("intelligence-feed", _embed(
        focus,
        COLOR_GOLD,
        footer=f"morning brief · {_now_et()}",
    ))


# ── Channel guides ────────────────────────────────────────────────────────────
# Each guide is pinned at the top of its channel on bot startup.

# ── 🧠 INTELLIGENCE ───────────────────────────────────────────────────────────

_GUIDE_MORNING_BRIEF = (
    "**Kal — #morning-brief**\n"
    "Posted every morning between 5:30–9am ET. Kal reads 5 financial newsletters and synthesizes them into one brief.\n\n"
    "**What you'll see:** Macro headlines with trade implications · Crypto signals · AI & tech moves · Big institutional flows · "
    "Sector breakdown · The day's best trade setup · Which Kalshi markets connect to today's news.\n\n"
    "One post per morning. Today's key insight also cross-posted to #intelligence-feed."
)

_GUIDE_BREAKING_NEWS = (
    "**Kal — #breaking-news**\n"
    "Real-time alerts when major market-moving events happen.\n\n"
    "**What you'll see:** Breaking headlines from financial news sources · Which Kalshi markets are affected · "
    "Current crowd pricing on those markets.\n\n"
    "Only posts when something significant happens. Not a firehose."
)

_GUIDE_BIG_MONEY = (
    "**Kal — #big-money**\n"
    "Tracks where large capital is flowing: institutions, bonds, commodities, sector rotations.\n\n"
    "**What you'll see:** Unusual options activity · Bond yield moves · Gold and oil flows · "
    "Sector rotation signals · Institutional positioning changes.\n\n"
    "Signal-to-noise focused. Only posts when capital movement is significant."
)

_GUIDE_THESIS = (
    "**Kal — #thesis**\n"
    "Kal's longer-form market reads with full reasoning laid out.\n\n"
    "**What you'll see:** Multi-paragraph analysis on specific setups · Why Kal thinks the crowd is wrong · "
    "Full chain of reasoning from data to conclusion.\n\n"
    "Rare — only posted when Kal has high conviction with a lot to say."
)

# ── 📊 MARKETS ────────────────────────────────────────────────────────────────

_GUIDE_TRADES = (
    "**Kal — #trades**\n"
    "Every trade Kal places and the result when it settles.\n\n"
    "**Trade Placed** — Kal put money on a market. Shows coin, direction, amount, odds, and what needs to happen to win.\n"
    "**Win** — Trade resolved in Kal's favor. Shows profit and updated record.\n"
    "**Loss** — Trade resolved against Kal. Shows the loss and running record.\n"
    "**Position Update** — Snapshot of all currently open trades.\n\n"
    "All trades are paper (simulated) until Kal is switched to live mode."
)

_GUIDE_WATCHLIST = (
    "**Kal — #watchlist**\n"
    "Markets Kal is eyeing but hasn't traded yet. Worth watching.\n\n"
    "**What you'll see:** Markets with interesting setups that don't quite meet Kal's trade threshold · "
    "Why the crowd might be wrong · What needs to change to make this a real trade.\n\n"
    "Think of this as Kal's scratch pad — interesting but not yet actionable."
)

_GUIDE_WEEKLY = (
    "**Kal — #weekly-analysis**\n"
    "Posted every Monday at ~8am ET.\n\n"
    "**What you'll see:** Last week's trading performance · Win rate and P&L breakdown · "
    "Market review for the past week · Top setups for the week ahead · Kalshi opportunities on Kal's radar.\n\n"
    "One post per week. Covers crypto, macro, and Kalshi prediction markets."
)

# ── 📈 ASSET CLASSES ──────────────────────────────────────────────────────────

_GUIDE_CRYPTO = (
    "**Kal — #crypto**\n"
    "Bitcoin, Ethereum, and Solana analysis — TA updates, Kal's market reads, and on-chain signals.\n\n"
    "**What you'll see:** Hourly technical analysis for BTC, ETH, SOL · "
    "Price action reads (support/resistance, trend, momentum) · "
    "Kal's probability assessment vs. Kalshi crowd pricing · Volume signals.\n\n"
    "Updated every 30–60 minutes while markets are active."
)

_GUIDE_STOCKS = (
    "**Kal — #stocks**\n"
    "Equity market moves, earnings, and sector analysis with prediction market implications.\n\n"
    "**What you'll see:** Sector strength/weakness · Key stock movers · Earnings beats/misses · "
    "S&P 500 and Nasdaq reads · How stock moves connect to Kalshi markets.\n\n"
    "Posted when notable moves or catalysts appear."
)

_GUIDE_PREDICTION_MARKETS = (
    "**Kal — #prediction-markets**\n"
    "Kalshi-specific analysis — which markets look mispriced, crowd behavior, and opportunity mapping.\n\n"
    "**What you'll see:** Deep dives on specific Kalshi markets · Crowd pricing vs. Kal's model · "
    "Which contracts have the most edge · Market mechanics and liquidity analysis.\n\n"
    "Kalshi-focused. The core of what Kal does."
)

_GUIDE_COMMODITIES = (
    "**Kal — #commodities**\n"
    "Oil, gold, silver, and broader commodities analysis.\n\n"
    "**What you'll see:** WTI and Brent crude reads · Gold as a macro signal · "
    "Silver and industrial metals · Commodity price implications for prediction markets.\n\n"
    "Posted when commodity moves are significant enough to affect markets."
)

# ── 🚨 SIGNALS ────────────────────────────────────────────────────────────────

_GUIDE_HIGH_CONVICTION = (
    "**Kal — #high-conviction**\n"
    "Only Kal's strongest setups. Rare. When this posts, pay attention.\n\n"
    "**What you'll see:** Markets pricing at >92% or <8% with real volume — "
    "extreme crowd conviction with money behind it · "
    "Kal's assessment of whether that conviction is justified.\n\n"
    "Posts maybe 1–3 times per day maximum. Not frequent by design."
)

_GUIDE_INTELLIGENCE_FEED = (
    "**Kal — #intelligence-feed**\n"
    "Live market surveillance — price shifts, volume spikes, new windows, and market snapshots.\n\n"
    "**Price Shift** — Crowd moved 10+ points since last scan. Opinion is changing fast.\n"
    "**Volume Spike** — Trading volume doubled in one window. Real money flowing in.\n"
    "**New Window** — Fresh 15-minute market opened with significant volume already in.\n"
    "**Market Snapshot** — Hourly summary of all active markets with current pricing.\n"
    "**Today's Focus** — Morning brief key insight cross-posted here.\n\n"
    "Alerts fire at most once per hour per market. High signal, low noise."
)

# ── 📅 CALENDAR ───────────────────────────────────────────────────────────────

_GUIDE_ECONOMIC_CALENDAR = (
    "**Kal — #economic-calendar**\n"
    "Posted every Sunday at 8am ET with the full week ahead — high impact events, "
    "Fed meeting dates, CPI releases, jobs reports, bond market readings, and Kalshi markets to watch.\n\n"
    "**What you'll see:** High and medium impact economic events for the week · "
    "Current bond market readings (yields, curve, credit spreads) · "
    "Key macro data (CPI, unemployment, oil, gold) · "
    "Kalshi markets that connect to this week's events · Theme for the week.\n\n"
    "Daily alerts posted to #morning-brief on weekdays when major economic events are scheduled that day."
)

# ── 🔄 DAILY ROUTINE ──────────────────────────────────────────────────────────

_GUIDE_MARKET_OPEN = (
    "**Kal — #market-open**\n"
    "Posted at 9:30am ET every weekday. Where markets are opening — crypto, bonds, gold, oil — "
    "and the one thing worth watching today.\n\n"
    "**What you'll see:** Bitcoin, Ethereum, Solana with overnight changes · "
    "Gold, oil, and dollar index levels · "
    "10Y and 2Y Treasury yields with yield curve status · "
    "Biggest overnight headline · Today's watch item.\n\n"
    "Pure data. No fluff. Everything you need in 30 seconds."
)

_GUIDE_MARKET_CLOSE = (
    "**Kal — #market-close**\n"
    "Posted at 4:00pm ET every weekday. Kal's full daily reflection.\n\n"
    "**What you'll see:** What moved and why · Where the bond market went and what it means · "
    "Where institutional money rotated · Trade opportunities Kal spots across all markets · "
    "Kal's own trade results for the day (WIN/LOSS) · What Kal learned today and how he's improving.\n\n"
    "This is where Kal gets better every day. One Claude call per day."
)

_GUIDE_IDEAS = (
    "**Kal — #ideas**\n"
    "Private channel. Kal brings opportunities here that are outside his current trading mandate.\n\n"
    "Kal executes Kalshi prediction markets and crypto trades autonomously. "
    "He posts here only when he sees an opportunity in stocks, bonds, commodities, "
    "or wants to request a mandate expansion.\n\n"
    "**How it works:** Kal posts an idea with full context, the specific trade he'd make, "
    "and the risks. Reply **APPROVED** to let him proceed, or **PASS** to decline.\n\n"
    "Maximum one idea per day. Only genuine high conviction setups."
)

# ── ⚙️ SYSTEM ─────────────────────────────────────────────────────────────────

_GUIDE_SUMMARY = (
    "**Kal — #summary**\n"
    "Performance reports, P&L updates, and cost tracking.\n\n"
    "**Check In** — Posted every 3 hours. Shows markets scanned, trades placed, wins, losses, P&L.\n"
    "**Daily Report** — End-of-day breakdown with Excel scorecard attached.\n"
    "**Research Complete** — After a full market scan with top opportunities found.\n"
    "**Daily Cost Report** — How much Kal spent on Claude API calls today.\n\n"
    "System-level updates only. No market analysis here."
)

_GUIDE_ALERTS = (
    "**Kal — #alerts**\n"
    "Technical issues only. If something is wrong with the bot, it shows up here.\n\n"
    "**Online/Offline** — Kal started up or shut down.\n"
    "**Out of Credits** — Anthropic API credits exhausted. Analysis paused.\n"
    "**Cost Warning** — Approaching daily spending limit.\n"
    "**Error** — Something went wrong technically. Kal is still running.\n"
    "**Order Rejected** — Kalshi rejected a trade order.\n\n"
    "Should be quiet most of the time. Noise here = something needs attention."
)


# ── Market Intelligence helpers ────────────────────────────────────────────────

def _format_close(close_time: str) -> str:
    """Convert ISO close_time to 'closes 3:45pm ET'. Returns '' on error."""
    if not close_time:
        return ""
    try:
        ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return f"closes {_utc_to_et(ct)}"
    except Exception:
        return ""


def _crowd_direction(yes_price: float) -> str:
    """Plain English crowd read for a yes probability."""
    if yes_price > 0.60:
        return "crowd thinks UP"
    if yes_price < 0.40:
        return "crowd thinks DOWN"
    return "crowd is split"


def _intel_footer(snap: dict) -> str:
    """Coin + close time footer — never exposes raw ticker strings."""
    coin      = snap.get("coin", "")
    coin_full = COIN_FULL.get(coin.upper(), coin or "market")
    close_str = _format_close(snap.get("close_time", ""))
    if close_str:
        return f"{coin_full} 15min · {close_str} · {_now_et()}"
    return f"{coin_full} 15min · {_now_et()}"


# ── Market Intelligence notifications ─────────────────────────────────────────

async def notify_intelligence_price_move(snap: dict, prev_price: float) -> None:
    """Yes/No price shifted more than 10 percentage points since last scan."""
    coin      = snap.get("coin", "")
    coin_full = COIN_FULL.get(coin.upper(), coin or "?")
    curr      = snap["yes_price"]
    shift     = curr - prev_price
    direction = "UP" if shift > 0 else "DOWN"
    curr_pct  = round(curr * 100)
    prev_pct  = round(prev_price * 100)
    shift_pct = round(abs(shift) * 100)

    crowd_read = _crowd_direction(curr)
    await _send("intelligence-feed", _embed(
        f"**Price Shift — {coin_full}**\n"
        f"Crowd moved **{shift_pct} points {direction}** since last scan.\n"
        f"Was: {prev_pct}% → Now: **{curr_pct}%** ({crowd_read})\n"
        f"Volume: ${snap['volume']:,.0f}",
        COLOR_ORANGE,
        footer=_intel_footer(snap),
    ))


async def notify_intelligence_volume_spike(snap: dict, prev_volume: float) -> None:
    """Volume at least doubled in one scan window."""
    coin      = snap.get("coin", "")
    coin_full = COIN_FULL.get(coin.upper(), coin or "?")
    mult      = snap["volume"] / prev_volume if prev_volume else 0
    crowd_pct = round(snap["yes_price"] * 100)
    crowd_read = _crowd_direction(snap["yes_price"])

    await _send("intelligence-feed", _embed(
        f"**Volume Spike — {coin_full}**\n"
        f"Money flowing in fast — volume jumped **{mult:.1f}×** this window.\n"
        f"Was: ${prev_volume:,.0f} → Now: **${snap['volume']:,.0f}**\n"
        f"Crowd pricing: {crowd_pct}% ({crowd_read})",
        COLOR_GOLD,
        footer=_intel_footer(snap),
    ))


async def notify_intelligence_new_market(snap: dict) -> None:
    """New 15-minute window opened with meaningful volume already in."""
    coin      = snap.get("coin", "")
    coin_full = COIN_FULL.get(coin.upper(), coin or "?")
    crowd_pct = round(snap["yes_price"] * 100)

    close_str = _format_close(snap.get("close_time", ""))
    close_line = f"{close_str} window just opened." if close_str else "New 15-minute window just opened."
    crowd_read = _crowd_direction(snap["yes_price"])
    direction_word = "UP" if snap["yes_price"] > 0.60 else "DOWN" if snap["yes_price"] < 0.40 else "NEITHER WAY"

    await _send("intelligence-feed", _embed(
        f"**New Window — {coin_full}**\n"
        f"{close_line} ${snap['volume']:,.0f} already traded.\n"
        f"Crowd is pricing {coin_full} {direction_word} ({crowd_pct}% chance it goes up).\n"
        f"Worth watching — real money already in.",
        COLOR_BLUE,
        footer=_intel_footer(snap),
    ))


async def notify_intelligence_high_conviction(snap: dict) -> None:
    """Crowd is pricing the outcome at <8% or >92% with real volume and time remaining."""
    coin      = snap.get("coin", "")
    coin_full = COIN_FULL.get(coin.upper(), coin or "?")
    crowd_pct = round(snap["yes_price"] * 100)

    if snap["yes_price"] >= 0.92:
        direction = "UP"
        rarity_line = f"Pricing: {crowd_pct}% chance of going up | Volume: ${snap['volume']:,.0f}"
        color = COLOR_GREEN
    else:
        direction = "DOWN"
        rarity_line = f"Pricing: {crowd_pct}% chance of going up | Volume: ${snap['volume']:,.0f}"
        color = COLOR_RED

    await _send("high-conviction", _embed(
        f"**Strong Signal — {coin_full}**\n"
        f"Crowd is extremely confident {coin_full} goes {direction} this window.\n"
        f"{rarity_line}\n"
        f"This level of certainty is rare in 15-minute markets.",
        color,
        footer=_intel_footer(snap),
    ))


async def notify_intelligence_summary(
    markets: list[dict],
    live_prices: dict[str, float] | None = None,
) -> None:
    """Snapshot of all active 15-minute markets — plain English format."""
    if live_prices is None:
        live_prices = {}

    if not markets:
        await _send("intelligence-feed", _embed(
            "**Market Snapshot** — No active 15-minute markets right now.",
            COLOR_GRAY,
            footer=f"intelligence · {_now_et()}",
        ))
        return

    lines = [f"**Market Snapshot — {_now_et()}**"]
    for snap in markets:
        coin      = snap.get("coin", "")
        coin_full = COIN_FULL.get(coin.upper(), coin or snap.get("ticker", "?"))
        crowd_pct = round(snap["yes_price"] * 100)
        vol_str   = f"${snap['volume']:,.0f}" if snap["volume"] > 0 else "$0"
        crowd_read = _crowd_direction(snap["yes_price"])

        price = live_prices.get(coin.upper(), 0.0)
        if price:
            price_str = f"${price:,.2f}" if price < 1000 else f"${price:,.0f}"
            lines.append(f"{coin_full} {price_str} — {crowd_read} ({crowd_pct}% up) | {vol_str} traded")
        else:
            lines.append(f"{coin_full} — {crowd_read} ({crowd_pct}% up) | {vol_str} traded")

    await _send("intelligence-feed", _embed(
        "\n".join(lines),
        COLOR_BLUE,
        footer=f"intelligence · {_now_et()}",
    ))


# ── New daily-routine notifications ──────────────────────────────────────────

async def notify_economic_calendar_weekly(content: str) -> None:
    """Post the Sunday week-ahead economic calendar to #economic-calendar."""
    await _send("economic-calendar", {"content": content, "username": KAL})


async def notify_daily_calendar_alert(alert: str) -> None:
    """Post a one-line economic event alert to #morning-brief (weekday 7am)."""
    await _send("morning-brief", {"content": alert, "username": KAL})


async def notify_market_open(content: str) -> None:
    """Post the 9:30am market open snapshot to #market-open."""
    await _send("market-open", {"content": content, "username": KAL})


async def notify_market_close(content: str) -> None:
    """Post the 4:00pm market close reflection to #market-close."""
    await _send("market-close", {"content": content, "username": KAL})


async def notify_idea(idea_text: str) -> None:
    """Post an #ideas flagging post to the private #ideas channel."""
    await _send("ideas", {"content": idea_text, "username": KAL})


async def send_channel_guide() -> None:
    """
    Called once at bot startup (after the "Kal online" alert).

    When DISCORD_BOT_TOKEN is set:
      1. Update bot profile to username "Kal" with the green logo avatar
      2. Find or create all 7 categories and 19 sub-channels
      3. Post guide cards to any channel that doesn't have one yet (idempotent)
      4. Pin each guide card (best-effort)
      5. Cache channel IDs so all future _send() calls go through the bot

    When bot token is absent, falls back to posting guide cards via webhooks
    (no pinning, no channel creation).
    """
    global _bot

    token = getattr(settings, "discord_bot_token", "")

    # Full guide map — all 19 channels
    guides = {
        # 🧠 INTELLIGENCE
        "morning-brief":       _GUIDE_MORNING_BRIEF,
        "breaking-news":       _GUIDE_BREAKING_NEWS,
        "big-money":           _GUIDE_BIG_MONEY,
        "thesis":              _GUIDE_THESIS,
        # 📅 CALENDAR
        "economic-calendar":   _GUIDE_ECONOMIC_CALENDAR,
        # 🔄 DAILY ROUTINE
        "market-open":         _GUIDE_MARKET_OPEN,
        "market-close":        _GUIDE_MARKET_CLOSE,
        "ideas":               _GUIDE_IDEAS,
        # 📊 MARKETS
        "trades":              _GUIDE_TRADES,
        "watchlist":           _GUIDE_WATCHLIST,
        "weekly-analysis":     _GUIDE_WEEKLY,
        # 📈 ASSET CLASSES
        "crypto":              _GUIDE_CRYPTO,
        "stocks":              _GUIDE_STOCKS,
        "prediction-markets":  _GUIDE_PREDICTION_MARKETS,
        "commodities":         _GUIDE_COMMODITIES,
        # 🚨 SIGNALS
        "high-conviction":     _GUIDE_HIGH_CONVICTION,
        "intelligence-feed":   _GUIDE_INTELLIGENCE_FEED,
        # ⚙️ SYSTEM
        "summary":             _GUIDE_SUMMARY,
        "alerts":              _GUIDE_ALERTS,
    }

    if token:
        from discord_bot import DiscordBot, make_kal_avatar
        bot = DiscordBot(token)

        # Update bot profile
        try:
            avatar = make_kal_avatar()
            await bot.update_profile("Kal", avatar)
        except Exception as exc:
            log.warning("[discord] profile update failed: %s", exc)

        # Create channels + categories, pin guides
        try:
            await bot.setup(guides)
            _bot = bot
            log.info("[discord] channel setup complete — routing through bot API")
        except Exception as exc:
            log.warning("[discord] channel setup failed: %s", exc)

    else:
        # No bot token — send guide cards via webhooks only (no pinning)
        log.warning("[discord] DISCORD_BOT_TOKEN not set — sending guides via webhook (no pinning)")
        for ch_name, guide_text in guides.items():
            await _send(ch_name, {"content": guide_text, "username": KAL})
