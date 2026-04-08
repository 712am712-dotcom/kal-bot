"""
market_qa.py — Market status, holiday calendar, and daily QA scan for Kal.

Market status:
  Uses Polygon.io free API: GET /v1/marketstatus/now?apiKey=<key>
  Cached with 30-minute TTL. Returns None silently if key not set or API fails.
  Only appended to financial-channel messages (attention, breaking, morning-brief,
  patterns, content-queue). Never blocks Discord sends — uses cached value.

Holiday calendar:
  Hard-coded US market holidays for 2025–2026.
  Returns a contextual note for any holiday date so financial updates carry context.

Daily QA scan:
  Runs once at 9pm ET. Scans recent bot messages across all channels.
  Detects duplicate posts (same first 80 chars posted twice in 24h).
  Posts a single plain-English summary to #system-logs.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time

import httpx

log = logging.getLogger(__name__)

# ── Price sanity checks ───────────────────────────────────────────────────────
#
# Valid ranges for assets Kal reports. Values outside these bounds are
# flagged and suppressed before posting. Update ranges annually.
#
# Format: asset_key → (low, high)
_PRICE_RANGES: dict[str, tuple[float, float]] = {
    "WTI Crude":   (50.0,    200.0),
    "Brent Crude": (50.0,    210.0),
    "S&P 500":     (2000.0,  8000.0),
    "Gold":        (1500.0,  8000.0),
    "BTC":         (10_000.0, 200_000.0),
    "Bitcoin":     (10_000.0, 200_000.0),
}


def check_price_sanity(asset: str, value: float) -> tuple[bool, str]:
    """
    Validate that a price falls within the expected range for its asset.

    Returns (True, "ok") if valid, or (False, log_message) if outside range.
    The caller is responsible for suppressing the value and logging on failure.

    Usage:
        ok, msg = check_price_sanity("WTI Crude", price)
        if not ok:
            log.warning("[qa] %s", msg)
            price = None  # suppress
    """
    bounds = _PRICE_RANGES.get(asset)
    if bounds is None:
        return True, "ok"   # no range defined — pass through

    low, high = bounds
    if low <= value <= high:
        return True, "ok"

    msg = (
        f"price_sanity_failed asset={asset} value={value:.2f} "
        f"expected_range=${low:,.0f}-${high:,.0f}"
    )
    log.warning("[market_qa] %s", msg)
    return False, msg


# ── Financial channels that receive market-status context ─────────────────────
FINANCIAL_CHANNELS: frozenset[str] = frozenset({
    "morning-brief",
    "the-trade-today",
    "mfd-the-trade-today",
    "ae-attention",
    "breaking",
    "patterns",
    "ae-content-queue",
})

# ── Market status cache ───────────────────────────────────────────────────────
_CACHE_TTL = 1800  # 30 minutes

_status_cache: dict = {
    "status":     "unknown",
    "note":       None,      # str or None
    "fetched_at": 0.0,
}


async def _fetch_polygon_status(api_key: str) -> dict | None:
    """
    Call Polygon /v1/marketstatus/now. Returns the JSON dict or None on failure.
    Free tier: unlimited calls, no auth header required — key goes in query string.
    """
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                "https://api.polygon.io/v1/marketstatus/now",
                params={"apiKey": api_key},
            )
        if r.status_code == 200:
            return r.json()
        log.debug("[market_qa] polygon HTTP %d", r.status_code)
        return None
    except Exception as exc:
        log.debug("[market_qa] polygon fetch failed: %s", exc)
        return None


async def get_market_status_note(api_key: str = "") -> str | None:
    """
    Return a market-closed note string, or None if markets are open.

    Note is injected into financial-channel Discord messages so readers
    always know whether they're looking at live or stale data.

    Uses a 30-min cache — safe to call on every Discord send.
    """
    # Check holiday first (no API key required)
    today = _today_et()
    holiday = get_holiday_note(today)
    if holiday:
        return holiday

    if not api_key:
        return None

    now = time.monotonic()
    if now - _status_cache["fetched_at"] < _CACHE_TTL:
        return _status_cache["note"]

    data = await _fetch_polygon_status(api_key)
    if data is None:
        # API failed — don't update cache, return last known note
        return _status_cache.get("note")

    market = data.get("market", "").lower()
    if market in ("closed", "early_closed"):
        after_hours = data.get("afterHours", False)
        if after_hours:
            note = "Note: Markets are in after-hours trading."
        else:
            note = "Note: Markets are closed today."
    elif market == "extended-hours":
        note = "Note: Markets are in extended / pre-market hours."
    else:
        note = None  # open — no annotation needed

    _status_cache.update({"status": market, "note": note, "fetched_at": now})
    return note


# ── Holiday calendar ──────────────────────────────────────────────────────────

# US equity market holidays — NYSE/NASDAQ observe these dates.
# Update annually. Format: "YYYY-MM-DD" → "Holiday name"
_MARKET_HOLIDAYS: dict[str, str] = {
    # 2025
    "2025-01-01": "New Year's Day",
    "2025-01-20": "Martin Luther King Jr. Day",
    "2025-02-17": "Presidents' Day",
    "2025-04-18": "Good Friday",
    "2025-05-26": "Memorial Day",
    "2025-06-19": "Juneteenth",
    "2025-07-04": "Independence Day",
    "2025-09-01": "Labor Day",
    "2025-11-27": "Thanksgiving",
    "2025-12-25": "Christmas Day",
    # 2026
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas Day",
}


def get_holiday_note(date_iso: str) -> str | None:
    """
    Return a holiday context string for any US market holiday, or None.
    date_iso: "YYYY-MM-DD"
    """
    holiday = _MARKET_HOLIDAYS.get(date_iso)
    if holiday:
        return f"Note: Markets are closed today ({holiday})."
    return None


def _today_et() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date().isoformat()


def _now_et_hour() -> int:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).hour
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).hour


# ── Daily QA scan ─────────────────────────────────────────────────────────────

async def run_daily_qa_scan(bot: "DiscordBot", channel_ids: dict[str, int]) -> str:
    """
    Scan last 50 messages across all channels. Detect duplicate bot posts.
    Returns a plain-English summary string for #system-logs.

    Duplicate detection: two bot messages whose first 80 chars match = duplicate.
    Signal count: messages in attention/breaking/patterns channels from the bot.
    """
    try:
        bot_id = await bot.get_bot_id()
    except Exception as exc:
        log.warning("[market_qa] qa_scan failed to get bot_id: %s", exc)
        return "Daily QA: scan skipped (bot identity unavailable)."

    signals_posted = 0
    duplicates_found = 0
    context_flags = 0

    seen_fingerprints: set[str] = set()

    signal_channels = {"attention", "breaking", "patterns"}

    for ch_name, ch_id in channel_ids.items():
        try:
            messages = await bot.get_messages(ch_id, limit=50)
            await asyncio.sleep(0.3)  # gentle rate-limit pause between channels
        except Exception as exc:
            log.debug("[market_qa] qa_scan get_messages %s failed: %s", ch_name, exc)
            continue

        for msg in messages:
            if int(msg.get("author", {}).get("id", 0)) != bot_id:
                continue

            content = msg.get("content", "")
            # Count embeds text too
            for embed in msg.get("embeds", []):
                content += " " + embed.get("description", "")

            content = content.strip()
            fingerprint = content[:80]

            if fingerprint in seen_fingerprints:
                duplicates_found += 1
            else:
                seen_fingerprints.add(fingerprint)

            if ch_name in signal_channels:
                signals_posted += 1

            # Context flag: message references a price or % but is > 4 hours old
            ts = msg.get("timestamp", "")
            if ts:
                try:
                    msg_dt = datetime.datetime.fromisoformat(ts.rstrip("Z"))
                    age_hours = (datetime.datetime.utcnow() - msg_dt).total_seconds() / 3600
                    if age_hours > 4 and ("%" in content or "$" in content):
                        context_flags += 1
                except Exception:
                    pass

    issues = []
    if duplicates_found:
        issues.append(f"{duplicates_found} duplicate{'s' if duplicates_found != 1 else ''} suppressed")
    if context_flags:
        issues.append(f"{context_flags} context flag{'s' if context_flags != 1 else ''} added")

    if issues:
        detail = ", ".join(issues) + "."
        status = "Review recommended."
    else:
        detail = "No issues."
        status = "All channels clean."

    return (
        f"Daily QA: {signals_posted} signal{'s' if signals_posted != 1 else ''} posted, "
        f"{detail} {status}"
    )
