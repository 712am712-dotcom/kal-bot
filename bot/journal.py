"""
journal.py — Kal paper trading journal.

Every BUY decision and every resolved market gets logged to kal_journal.txt
in a human-readable format exactly like a trader's hand-written notebook.

Two files are maintained:
  kal_journal.txt         — the readable log (append-only)
  kal_journal_state.json  — running totals (wins, losses, P&L)
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path

_HERE        = Path(__file__).parent
JOURNAL_PATH = _HERE / "kal_journal.txt"
STATE_PATH   = _HERE / "kal_journal_state.json"

STARTING_BALANCE = 500.00
DIVIDER          = "─" * 52
THICK            = "=" * 52


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts() -> str:
    """Human timestamp: Mar 21 2026 - 3:45pm"""
    n    = datetime.now()
    hr   = str(int(n.strftime("%I")))   # strip leading zero portably
    mins = n.strftime("%M")
    ampm = n.strftime("%p").lower()
    date = n.strftime("%b %d %Y")
    return f"{date} - {hr}:{mins}{ampm}"


def _wrap(text: str, width: int = 58, indent: str = "  ") -> str:
    """Word-wrap a long string, indenting continuation lines."""
    lines = textwrap.wrap(text, width=width)
    return ("\n" + indent).join(lines)


def _pct(raw: str) -> str:
    """Convert a stored decimal string like '0.710000' → '71.0%'."""
    try:
        return f"{float(raw):.1%}"
    except (ValueError, TypeError):
        return str(raw)


def _cents_to_price(raw: str) -> str:
    """Convert stored yes_price_cents string '62' → '62¢  ($0.62)'."""
    try:
        c = int(raw)
        return f"{c}¢  (${c / 100:.2f} per contract)"
    except (ValueError, TypeError):
        return str(raw)


# ── State ──────────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "started":               datetime.now().strftime("%b %d %Y"),
        "starting_balance":      STARTING_BALANCE,
        "running_pnl":           0.0,
        "wins":                  0,
        "losses":                0,
        "total_research_trades": 0,
        "total_live_trades":     0,
    }


def _write_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _balance(state: dict) -> float:
    return state["starting_balance"] + state["running_pnl"]


# ── Journal init ───────────────────────────────────────────────────────────────

def _ensure_journal(state: dict) -> None:
    if JOURNAL_PATH.exists():
        return
    header = (
        f"{THICK}\n"
        f"KAL TRADING JOURNAL\n"
        f"Started: {state['started']}\n"
        f"Starting Balance: ${state['starting_balance']:,.2f}\n"
        f"{THICK}\n\n"
    )
    JOURNAL_PATH.write_text(header, encoding="utf-8")


def _append(text: str) -> None:
    with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
        fh.write(text)


# ── Public API ─────────────────────────────────────────────────────────────────

def log_session_start(mode: str, demo: bool) -> None:
    """Write a session-start entry."""
    state = _read_state()
    _ensure_journal(state)

    mode_label = "RESEARCH (Paper Trading)" if mode == "research" else "LIVE TRADING"
    env_label  = "Demo API (Paper)" if demo else "Live API (Real Money)"
    bal        = _balance(state)

    entry = (
        f"\n{DIVIDER}\n"
        f"[{_ts()}] SESSION STARTED\n"
        f"Mode:             {mode_label}\n"
        f"Environment:      {env_label}\n"
        f"Current Balance:  ${bal:,.2f}  "
        f"(started at ${state['starting_balance']:,.2f}, "
        f"P&L: ${state['running_pnl']:+,.2f})\n"
        f"Record:           {state['wins']}W / {state['losses']}L\n"
        f"{DIVIDER}\n"
    )
    _append(entry)


def log_trade_placed(
    analysis:      object,   # MarketAnalysis — avoid circular import
    decision:      dict,
    mode:          str,
    crypto_prices: dict | None = None,
) -> None:
    """Write a trade-placed entry."""
    state = _read_state()
    _ensure_journal(state)

    # Pull fields from MarketAnalysis
    ticker      = getattr(analysis, "ticker",          "?")
    title       = getattr(analysis, "market_title",    "?")
    coin        = getattr(analysis, "coin",             "")
    timeframe   = getattr(analysis, "timeframe",        "")
    rec         = getattr(analysis, "recommendation",   "?").replace("_", " ")
    confidence  = getattr(analysis, "confidence",       0.0)
    edge        = getattr(analysis, "edge",             0.0)
    kalshi_p    = getattr(analysis, "kalshi_yes_price", 0.0)
    claude_p    = getattr(analysis, "yes_probability",  0.0)
    strike      = getattr(analysis, "strike_price",     0.0)
    direction   = getattr(analysis, "direction",        "")
    live_price  = getattr(analysis, "live_price",       0.0)
    reasoning   = getattr(analysis, "reasoning",        "")

    side        = decision.get("side", "?").upper()
    contracts   = decision.get("contracts", 0)
    bet_dollars = decision.get("bet_dollars", 0.0)

    mode_label  = "PAPER TRADE" if mode.upper() == "RESEARCH" else "DEMO TRADE" if decision.get("demo") else "LIVE TRADE"

    # Spot price for this coin
    spot_str = ""
    if coin and crypto_prices:
        spot = crypto_prices.get(coin, {}).get("price", 0)
        chg  = crypto_prices.get(coin, {}).get("change_24h", 0)
        if spot:
            arrow = "▲" if chg >= 0 else "▼"
            spot_str = f"${spot:,.2f}  {arrow}{abs(chg):.2f}% (24h)"

    # Direction label
    vs_str = ""
    if strike and live_price:
        dist = ((live_price - strike) / strike) * 100
        vs_str = f"  ({dist:+.2f}% vs strike)"

    # Reasoning — first sentence only, trimmed
    short_reason = reasoning.split("|")[0].strip()[:280]
    wrapped_reason = _wrap(short_reason)

    state["total_research_trades" if mode.upper() == "RESEARCH" else "total_live_trades"] += 1
    _write_state(state)

    entry = (
        f"\n{DIVIDER}\n"
        f"[{_ts()}] {mode_label} PLACED\n"
        f"Market:      {title}\n"
        f"Ticker:      {ticker}\n"
    )
    if coin:
        entry += f"Coin:        {coin}  |  Timeframe: {timeframe}\n"
    entry += (
        f"Side:        BUY {side}\n"
        f"Entry Price: {round(kalshi_p * 100)}¢  (${kalshi_p:.2f} per contract)\n"
        f"Contracts:   {contracts}\n"
        f"Capital:     ${bet_dollars:.2f}\n"
        f"{DIVIDER[:20]}\n"
        f"Edge:        {edge:+.1%}\n"
        f"Confidence:  {confidence:.1%}\n"
        f"My Estimate: {claude_p:.1%}   (Kalshi crowd: {kalshi_p:.1%})\n"
    )
    if spot_str:
        entry += f"{coin} Spot:   {spot_str}\n"
    if strike:
        entry += f"Strike:      ${strike:,.2f} {direction}{vs_str}\n"
    entry += (
        f"{DIVIDER[:20]}\n"
        f"Reasoning:   {wrapped_reason}\n"
        f"{DIVIDER}\n"
    )
    _append(entry)


def log_trade_resolved(row: dict) -> None:
    """
    Write a resolution entry.
    row is a dict from PerformanceTracker.resolve_pending() — has all CSV columns.
    """
    state = _read_state()
    _ensure_journal(state)

    ticker    = row.get("_ticker",         row.get("Ticker", "?"))
    title     = row.get("Market Title",    "?")
    result    = row.get("Result",          "?")          # WIN | LOSS
    mode      = (row.get("Mode", "RESEARCH")).upper()
    rec       = row.get("Recommendation",  "?")          # BUY YES | BUY NO
    side      = row.get("_side",           "?")
    contracts = row.get("_contracts",      "0")
    ye_cents  = row.get("_yes_price_cents","?")
    bet_str   = row.get("Amount Wagered ($)", "0.00")
    pnl_str   = row.get("Profit/Loss ($)",    "0.00")
    conf_str  = row.get("Confidence %",       "0")
    edge_str  = row.get("Edge %",             "0")

    try:
        bet = float(bet_str)
        pnl = float(pnl_str)
    except ValueError:
        bet, pnl = 0.0, 0.0

    # Update running totals (only count LIVE trades for real P&L)
    if mode == "LIVE":
        state["running_pnl"] += pnl
        if result == "WIN":
            state["wins"] += 1
        elif result == "LOSS":
            state["losses"] += 1
    else:
        # Research: track accuracy separately
        if result == "WIN":
            state["wins"] += 1
        elif result == "LOSS":
            state["losses"] += 1
    _write_state(state)

    total_w  = state["wins"]
    total_l  = state["losses"]
    total_wl = total_w + total_l
    win_rate = (total_w / total_wl * 100) if total_wl else 0.0
    bal      = _balance(state)

    win_emoji = "✓ WIN" if result == "WIN" else "✗ LOSS"

    # Contracts payout (only meaningful for live trades; research = $0)
    try:
        entry_price = int(ye_cents) / 100
        payout      = int(contracts) * 1.0 if result == "WIN" else 0.0
        payout_str  = f"${payout:,.2f}  ($1.00 x {contracts} contracts)" if mode == "LIVE" else "N/A (paper)"
    except (ValueError, TypeError):
        payout_str = "N/A"

    pnl_label = f"{pnl:+.2f}" if mode == "LIVE" else f"{pnl:+.2f} (paper)"

    entry = (
        f"\n{DIVIDER}\n"
        f"[{_ts()}] TRADE RESOLVED  {win_emoji}\n"
        f"Ticker:      {ticker}\n"
        f"Market:      {title}\n"
        f"Result:      {result}\n"
        f"{DIVIDER[:20]}\n"
        f"Entry:       {ye_cents}¢  x  {contracts} contracts  =  ${bet:.2f} risked\n"
        f"Payout:      {payout_str}\n"
        f"Profit:      ${pnl_label}\n"
        f"{DIVIDER[:20]}\n"
        f"Running P&L: ${state['running_pnl']:+,.2f}\n"
        f"Balance:     ${bal:,.2f}\n"
        f"Record:      {total_w}W / {total_l}L  ({win_rate:.1f}% win rate)\n"
        f"{DIVIDER}\n"
    )
    _append(entry)


def log_error(context: str, error: str) -> None:
    """Log a notable error to the journal so the paper record is complete."""
    state = _read_state()
    _ensure_journal(state)
    entry = (
        f"\n{DIVIDER}\n"
        f"[{_ts()}] ERROR LOGGED\n"
        f"Context:  {context}\n"
        f"Error:    {_wrap(str(error)[:300])}\n"
        f"{DIVIDER}\n"
    )
    _append(entry)
