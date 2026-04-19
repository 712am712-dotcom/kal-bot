"""
economic_calendar.py — Economic calendar posts for Kal.

Two functions:
  1. post_weekly_calendar()   — Sunday 8am ET → #economic-calendar
  2. get_daily_calendar_alert() — Weekday 7am ET → #morning-brief if HIGH impact events today

Both use pure data sources — zero Claude API calls.
Data sources (all free):
  - Forex Factory calendar API (no key required) — primary economic events
  - Alpha Vantage EARNINGS_CALENDAR (free tier) — upcoming earnings
  - FRED API (already configured) — bond/yield data

Scheduling (called from main.py):
  Sunday 8am ET = Sunday 13:00 UTC
  Weekday 7am ET = Weekday 12:00 UTC  (approximate — exact check in caller)
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# State file — tracks which Sunday/weekday posts have already fired today
_STATE_PATH = Path(__file__).parent / "calendar_state.json"

# ── FRED client (lazy init) ────────────────────────────────────────────────────
_fred_client = None


def _get_fred(api_key: str):
    global _fred_client
    if _fred_client is None or _fred_client._api_key != api_key:
        from fred_client import FredClient
        _fred_client = FredClient(api_key)
    return _fred_client


# ── State helpers ─────────────────────────────────────────────────────────────

def _read_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("[calendar] state write failed: %s", exc)


def weekly_already_posted() -> bool:
    state = _read_state()
    today = datetime.date.today().isoformat()
    return state.get("weekly_posted_date") == today


def mark_weekly_posted() -> None:
    state = _read_state()
    state["weekly_posted_date"] = datetime.date.today().isoformat()
    _write_state(state)


def daily_alert_already_posted() -> bool:
    state = _read_state()
    today = datetime.date.today().isoformat()
    return state.get("daily_alert_date") == today


def mark_daily_alert_posted() -> None:
    state = _read_state()
    state["daily_alert_date"] = datetime.date.today().isoformat()
    _write_state(state)


def day_before_alert_already_posted() -> bool:
    """True if today's evening day-before preview has already fired."""
    state = _read_state()
    today = datetime.date.today().isoformat()
    return state.get("day_before_alert_date") == today


def mark_day_before_alert_posted() -> None:
    state = _read_state()
    state["day_before_alert_date"] = datetime.date.today().isoformat()
    _write_state(state)


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _is_sunday_8am_et() -> bool:
    """True between 13:00–13:59 UTC on Sundays (8am ET / EDT)."""
    now = _now_utc()
    return now.weekday() == 6 and now.hour == 13


def _is_monday_9am_et() -> bool:
    """True between 14:00–14:59 UTC on Mondays (9am ET / EDT).
    Fallback in case Sunday weekly post was missed due to a redeploy."""
    now = _now_utc()
    return now.weekday() == 0 and now.hour == 14


def _is_weekday_7am_et() -> bool:
    """True between 12:00–12:59 UTC on weekdays (7am ET / EDT)."""
    now = _now_utc()
    return now.weekday() < 5 and now.hour == 12


def _is_evening_preview_utc() -> bool:
    """True between 01:00–01:59 UTC (9pm EDT / 10pm EST).
    Used for the evening day-before macro event preview."""
    now = _now_utc()
    return now.hour == 1


# ── Free data source helpers ──────────────────────────────────────────────────

_FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_FF_NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
_FF_HEADERS   = {"User-Agent": "KalBot/1.0 (market-intelligence)"}


async def _fetch_ff_calendar(next_week: bool = False) -> list[dict]:
    """
    Fetch economic events from Forex Factory free calendar API.
    Returns normalised event dicts with keys: event, country, date, time_et, impact, forecast, previous.
    Filters to USD events only. No API key required.
    """
    url = _FF_NEXT_WEEK if next_week else _FF_THIS_WEEK
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, headers=_FF_HEADERS)
            r.raise_for_status()
            raw = r.json()

        events: list[dict] = []
        for item in raw:
            impact_raw = str(item.get("impact", "")).strip().title()
            if impact_raw in ("Holiday", ""):
                continue
            impact = {"High": "HIGH", "Medium": "MEDIUM", "Low": "LOW"}.get(impact_raw, "MEDIUM")
            country = str(item.get("country", "")).upper().strip()
            if country not in ("USD", "US", ""):
                continue  # US events only
            events.append({
                "event":    item.get("title", "Unknown"),
                "country":  country,
                "date":     item.get("date", ""),
                "time_et":  item.get("time", ""),
                "impact":   impact,
                "forecast": item.get("forecast", ""),
                "previous": item.get("previous", ""),
            })
        log.info("[calendar] forex factory %s: %d USD events",
                 "next week" if next_week else "this week", len(events))
        return events
    except Exception as exc:
        log.warning("[calendar] forex factory fetch failed: %s", exc)
        return []


async def _fetch_av_earnings(av_key: str) -> list[dict]:
    """
    Fetch upcoming earnings from Alpha Vantage EARNINGS_CALENDAR (free tier, CSV).
    Returns at most 8 upcoming events as normalised dicts.
    """
    if not av_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                "https://www.alphavantage.co/query",
                params={"function": "EARNINGS_CALENDAR", "horizon": "1month", "apikey": av_key},
            )
            r.raise_for_status()
        import csv, io
        rows = list(csv.DictReader(io.StringIO(r.text)))
        events: list[dict] = []
        for row in rows[:8]:
            symbol = row.get("symbol", "")
            name   = row.get("name", symbol)
            est    = row.get("estimate", "")
            events.append({
                "event":    f"Earnings: {name} ({symbol})",
                "country":  "USD",
                "date":     row.get("reportDate", ""),
                "time_et":  "TBD",
                "impact":   "MEDIUM",
                "forecast": f"EPS est. {est}" if est else "",
                "previous": "",
            })
        log.info("[calendar] av earnings: %d upcoming", len(events))
        return events
    except Exception as exc:
        log.warning("[calendar] av earnings fetch failed: %s", exc)
        return []


def _impact_label(impact: str | None) -> str:
    """Normalise impact string to HIGH/MEDIUM/LOW."""
    if not impact:
        return "MEDIUM"
    s = str(impact).upper()
    if "HIGH" in s:
        return "HIGH"
    if "LOW" in s:
        return "LOW"
    return "MEDIUM"


def _format_event_time(event: dict) -> str:
    """Format event time as 'Mon, Apr 7 at 8:30am ET'. Handles FF format (separate date + time_et)."""
    date_str = event.get("date", "")
    time_str = str(event.get("time_et", "")).strip()

    try:
        if date_str:
            dt  = datetime.date.fromisoformat(date_str)
            day = dt.strftime("%a, %b %-d")
            if time_str and time_str not in ("Tentative", "All Day", "TBD", ""):
                return f"{day} at {time_str} ET"
            return day
    except Exception:
        pass
    return date_str or "TBD"


def _sort_impact(impact: str) -> int:
    return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(impact, 1)


# ── Kalshi market matching ────────────────────────────────────────────────────

_CALENDAR_KEYWORDS = {
    "cpi":        ["inflation", "CPI", "consumer price"],
    "fed":        ["federal reserve", "Fed", "FOMC", "interest rate", "Powell", "rate decision"],
    "jobs":       ["unemployment", "jobs", "payroll", "NFP", "nonfarm"],
    "gdp":        ["GDP", "gross domestic product"],
    "earnings":   ["earnings", "EPS", "revenue"],
    "oil":        ["oil", "crude", "OPEC"],
    "housing":    ["housing", "mortgage", "real estate", "home sales"],
}


def _find_kalshi_connections(events: list[dict], kalshi_markets: list[dict]) -> list[str]:
    """
    Match this week's high-impact events to open Kalshi markets.
    Returns up to 5 market titles worth watching.
    """
    if not kalshi_markets:
        return []

    high_events = [e for e in events if _impact_label(e.get("impact")) == "HIGH"]
    event_text  = " ".join(
        f"{e.get('event', '')} {e.get('country', '')}"
        for e in high_events
    ).lower()

    matches: list[str] = []
    for market in kalshi_markets:
        title = market.get("title", market.get("subtitle", ""))
        if not title:
            continue
        t = title.lower()
        # Match on keywords that link calendar events to Kalshi markets
        for keywords in _CALENDAR_KEYWORDS.values():
            if any(kw.lower() in event_text and kw.lower() in t for kw in keywords):
                matches.append(title)
                break
        if len(matches) >= 5:
            break
    return matches


# ── Weekly post ───────────────────────────────────────────────────────────────

async def post_weekly_calendar(
    fred_api_key: str,
    kalshi_markets: list[dict],
    av_key: str = "",
) -> str:
    """
    Build and return the weekly economic calendar Discord message.
    Caller sends this to #economic-calendar.
    Zero Claude calls — pure data.
    Sources: Forex Factory (free, no key) + Alpha Vantage earnings (free tier) + FRED bonds.
    """
    today     = datetime.date.today()
    # Week starting next Monday
    mon       = today + datetime.timedelta(days=(7 - today.weekday()) % 7)
    fri       = mon + datetime.timedelta(days=4)
    date_range = f"{mon.strftime('%b %-d')}–{fri.strftime('%b %-d, %Y')}"

    # Fetch in parallel
    import asyncio
    fred = _get_fred(fred_api_key)
    events_task   = _fetch_ff_calendar(next_week=True)
    earnings_task = _fetch_av_earnings(av_key)
    fred_task     = fred.get_all()
    ff_events, av_earnings, fred_data = await asyncio.gather(
        events_task, earnings_task, fred_task
    )
    events = ff_events + av_earnings

    # Sort and filter events
    events.sort(key=lambda e: (_sort_impact(_impact_label(e.get("impact"))), e.get("date", "")))
    high_events   = [e for e in events if _impact_label(e.get("impact")) == "HIGH"]
    medium_events = [e for e in events if _impact_label(e.get("impact")) == "MEDIUM"]

    # Kalshi market connections
    connections = _find_kalshi_connections(events, kalshi_markets)

    # Theme sentence (pure data logic, no Claude)
    theme = _derive_week_theme(fred_data, high_events)

    # ── Format message ──────────────────────────────────────────────────────
    lines = [f"**Week Ahead -- {date_range}**\n"]

    if high_events or medium_events:
        lines.append("**HIGH IMPACT EVENTS THIS WEEK:**")
        shown = 0
        for e in events:
            impact = _impact_label(e.get("impact"))
            if impact not in ("HIGH", "MEDIUM"):
                continue
            name    = e.get("event", "Unknown event")
            country = e.get("country", "")
            country_tag = f" ({country})" if country and country != "US" else ""
            time_str = _format_event_time(e)
            lines.append(f"- {time_str} -- {name}{country_tag} -- {impact}")
            shown += 1
            if shown >= 12:
                break
        lines.append("")

    # Bond market block
    if fred_data:
        lines.append(fred.format_bond_block(fred_data))
        lines.append("")
        lines.append(fred.format_macro_block(fred_data))
        lines.append("")
    else:
        lines.append("_Bond data unavailable — FRED_API_KEY not configured._\n")

    if connections:
        lines.append("**KALSHI MARKETS WORTH WATCHING THIS WEEK:**")
        for m in connections:
            lines.append(f"- {m}")
        lines.append("")

    lines.append(f"**Theme this week:** {theme}")

    return "\n".join(lines)


def _derive_week_theme(fred_data: dict, high_events: list[dict]) -> str:
    """
    Generate a 1-2 sentence theme for the week using only data signals.
    No Claude — pure conditional logic.
    """
    if not fred_data and not high_events:
        return "Monitor economic data as it releases and watch for any surprise deviations from consensus."

    parts: list[str] = []

    # Fed/rate events this week?
    fed_events = [e for e in high_events if any(
        kw in e.get("event", "").lower()
        for kw in ("fed", "fomc", "rate decision", "powell")
    )]
    if fed_events:
        parts.append("Fed activity this week is the dominant catalyst — watch for rate guidance language to move both bonds and equities.")

    # CPI/inflation events?
    cpi_events = [e for e in high_events if "cpi" in e.get("event", "").lower() or "inflation" in e.get("event", "").lower()]
    if cpi_events and not fed_events:
        parts.append("Inflation data this week is the key number — a surprise in either direction will reprice rate expectations.")

    # Jobs data?
    jobs_events = [e for e in high_events if any(
        kw in e.get("event", "").lower()
        for kw in ("payroll", "unemployment", "nonfarm", "jobs")
    )]
    if jobs_events and not fed_events and not cpi_events:
        parts.append("Jobs data this week will test the 'soft landing' thesis — strong prints support risk-on, weak prints raise recession flags.")

    # Bond signals
    if fred_data:
        inverted = fred_data.get("yield_curve_inverted", False)
        hy       = fred_data.get("hy_spread")
        if inverted:
            parts.append("The inverted yield curve continues to signal elevated recession risk -- this is the backdrop for every trade this week.")
        elif hy is not None and hy > 5.5:
            parts.append("Wide credit spreads suggest institutional caution -- this is not a week to be aggressive on risk.")

    if not parts:
        # Generic but data-aware fallback
        n = len(high_events)
        if n > 3:
            parts.append(f"A data-heavy week with {n} high-impact releases -- expect elevated volatility and watch for consensus misses.")
        else:
            parts.append("A quiet week on the calendar -- macro drift and bond yields will drive direction more than any single data point.")

    return " ".join(parts)


# ── Daily 7am alert ───────────────────────────────────────────────────────────

async def get_daily_calendar_alert(av_key: str = "") -> str | None:
    """
    Check today's Forex Factory calendar for HIGH impact USD events.
    Returns a one-line alert string for #morning-brief, or None if nothing significant.
    Zero Claude calls.
    """
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    events = await _fetch_ff_calendar(next_week=False)
    high   = [
        e for e in events
        if _impact_label(e.get("impact")) == "HIGH" and e.get("date", "") == today_str
    ]

    if not high:
        return None

    # Build a short alert
    alerts: list[str] = []
    for e in high[:3]:
        name     = e.get("event", "Economic Event")
        time_str = _format_event_time(e)
        why      = _event_why(name)
        alerts.append(f"**{name}** at {time_str}{' -- ' + why if why else ''}")

    return "**Today's Calendar:** " + " | ".join(alerts)


def _event_why(event_name: str) -> str:
    """Return a short 'why it matters' string for common event types."""
    n = event_name.lower()
    if "cpi" in n or "inflation" in n:
        return "inflation print moves rate expectations"
    if "fomc" in n or "fed" in n or "rate decision" in n:
        return "rate guidance will move bonds and risk assets"
    if "payroll" in n or "nonfarm" in n or "jobs" in n:
        return "jobs data tests the soft-landing thesis"
    if "gdp" in n:
        return "growth read affects recession probability"
    if "unemployment" in n:
        return "labor market health signal"
    if "pce" in n:
        return "Fed's preferred inflation gauge"
    if "ism" in n:
        return "manufacturing/services activity gauge"
    if "retail" in n:
        return "consumer spending health"
    return ""


def _event_market_implications(event_name: str) -> tuple[str, str]:
    """
    Return (hotter_than_expected, cooler_than_expected) market implication strings
    for a given event. Used in the day-before alert and morning-brief banner.
    """
    n = event_name.lower()
    if "cpi" in n:
        return (
            "bonds sell off, yields spike, stocks drop — rate cut odds fall",
            "rate cut odds rise, stocks rally, dollar weakens",
        )
    if "pce" in n:
        return (
            "Fed stays hawkish, equities under pressure",
            "rate cut expectations advance, risk-on rally",
        )
    if "ppi" in n:
        return (
            "upstream inflation pressures CPI outlook, yields rise",
            "inflation pipeline cools, bonds rally",
        )
    if "payroll" in n or "nonfarm" in n or "jobs" in n:
        return (
            "hot labor market → Fed stays higher for longer, growth stocks fall",
            "labor market cracks → recession risk rises, safe havens rally",
        )
    if "unemployment" in n:
        return (
            "tight labor market → Fed stays hawkish",
            "rising unemployment → recession risk, rate cuts priced in",
        )
    if "fomc" in n or "fed" in n or "rate decision" in n:
        return (
            "hawkish surprises → bonds sell off, yields spike, dollar up",
            "dovish tone → equities and crypto rally, dollar weakens",
        )
    if "gdp" in n:
        return (
            "strong growth → inflation fears, yields rise, dollar up",
            "weak growth → recession fears, safe havens rally",
        )
    if "retail" in n:
        return (
            "consumer spending strong → inflation sticky, yields up",
            "consumer weakening → recession concerns, rate cut bets rise",
        )
    return ("above-consensus → yields rise, risk-off", "below-consensus → yields fall, risk-on")


# ── Morning brief macro banner ─────────────────────────────────────────────────

async def build_today_macro_banner() -> str:
    """
    Returns a formatted ⚠️ banner for any HIGH impact USD events happening today.
    Prepended to the morning brief at build time.
    Returns "" if nothing significant today.

    Called from _gmail_brief_task immediately before build_morning_brief().
    Zero Claude calls.
    """
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    events = await _fetch_ff_calendar(next_week=False)
    high = [
        e for e in events
        if _impact_label(e.get("impact")) == "HIGH"
        and e.get("date", "") == today_str
    ]
    if not high:
        return ""

    lines: list[str] = []
    for e in high[:3]:
        name     = e.get("event", "Economic Event")
        time_str = e.get("time_et", "").strip()
        forecast = str(e.get("forecast", "")).strip()
        previous = str(e.get("previous", "")).strip()
        hot, cool = _event_market_implications(name)

        time_display = f" at **{time_str} ET**" if time_str and time_str not in ("Tentative", "All Day", "TBD", "") else ""
        est_line = ""
        if forecast or previous:
            parts = []
            if forecast:
                parts.append(f"Estimate: **{forecast}**")
            if previous:
                parts.append(f"Previous: {previous}")
            est_line = " | ".join(parts)

        lines.append(f"⚠️ **TODAY: {name}{time_display}**")
        if est_line:
            lines.append(est_line)
        lines.append(f"If hotter → {hot}.")
        lines.append(f"If cooler → {cool}.")
        lines.append("Watch this number above everything else today.")
        lines.append("─" * 48)

    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


# ── Evening day-before preview ─────────────────────────────────────────────────

async def get_tomorrow_high_impact_events() -> list[dict]:
    """
    Fetch HIGH impact USD events scheduled for tomorrow.
    Checks both this-week and next-week FF feeds (covers Sunday→Monday boundary).
    Returns list of event dicts or [].
    """
    tomorrow_str = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch both feeds in parallel — tomorrow might be in next week's feed
    import asyncio as _asyncio
    this_week, next_week = await _asyncio.gather(
        _fetch_ff_calendar(next_week=False),
        _fetch_ff_calendar(next_week=True),
    )
    all_events = this_week + next_week

    return [
        e for e in all_events
        if _impact_label(e.get("impact")) == "HIGH"
        and e.get("date", "") == tomorrow_str
    ]


async def get_today_high_impact_events() -> list[dict]:
    """
    Fetch HIGH impact USD events scheduled for today.
    Returns list of event dicts (same shape as get_tomorrow_high_impact_events).
    """
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    events = await _fetch_ff_calendar(next_week=False)
    return [
        e for e in events
        if _impact_label(e.get("impact")) == "HIGH"
        and e.get("date", "") == today_str
    ]


def build_day_before_alert(events: list[dict]) -> str:
    """
    Format the evening day-before macro preview.
    Returns "" if events list is empty.
    Posted to #morning-brief and #mfd-morning-brief at ~9pm ET.
    """
    if not events:
        return ""

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    day_label = tomorrow.strftime("%A, %B ") + str(tomorrow.day)

    lines = [f"📅 **Tomorrow — {day_label}**", ""]

    for e in events[:3]:
        name     = e.get("event", "Economic Event")
        time_str = e.get("time_et", "").strip()
        forecast = str(e.get("forecast", "")).strip()
        previous = str(e.get("previous", "")).strip()
        why      = _event_why(name)
        hot, cool = _event_market_implications(name)

        time_display = f"**{time_str} ET**" if time_str and time_str not in ("Tentative", "All Day", "TBD", "") else "**scheduled**"
        lines.append(f"**{name}** at {time_display}")
        if why:
            lines.append(f"_{why.capitalize()}_")
        if forecast or previous:
            parts = []
            if forecast:
                parts.append(f"Estimate: **{forecast}**")
            if previous:
                parts.append(f"Previous: {previous}")
            lines.append(" | ".join(parts))
        lines.append(f"If hotter → {hot}.")
        lines.append(f"If cooler → {cool}.")
        lines.append("")

    lines.append("_Set your alarm. These numbers move markets._")
    return "\n".join(lines)
