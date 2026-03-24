"""
economic_calendar.py — Economic calendar posts for Kal.

Two functions:
  1. post_weekly_calendar()   — Sunday 8am ET → #economic-calendar
  2. check_daily_calendar_alert() — Weekday 7am ET → #morning-brief if HIGH impact events today

Both use pure data sources — zero Claude API calls.
Data: Finnhub economic calendar + FRED bond data.

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


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _is_sunday_8am_et() -> bool:
    """True between 13:00–13:59 UTC on Sundays (8am ET)."""
    now = _now_utc()
    return now.weekday() == 6 and now.hour == 13


def _is_weekday_7am_et() -> bool:
    """True between 12:00–12:59 UTC on weekdays (7am ET)."""
    now = _now_utc()
    return now.weekday() < 5 and now.hour == 12


# ── Finnhub helpers ───────────────────────────────────────────────────────────

async def _fetch_finnhub_calendar(api_key: str, from_date: str, to_date: str) -> list[dict]:
    """Fetch economic calendar from Finnhub for the given date range."""
    if not api_key:
        return []
    url = "https://finnhub.io/api/v1/calendar/economic"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, params={"from": from_date, "to": to_date, "token": api_key})
            r.raise_for_status()
            data = r.json()
        events = data.get("economicCalendar", [])
        log.info("[calendar] finnhub returned %d events", len(events))
        return events
    except Exception as exc:
        log.warning("[calendar] finnhub fetch failed: %s", exc)
        return []


def _impact_label(impact: str | None) -> str:
    """Normalize Finnhub impact string to HIGH/MEDIUM/LOW."""
    if not impact:
        return "MEDIUM"
    s = str(impact).upper()
    if "HIGH" in s or s == "3":
        return "HIGH"
    if "LOW" in s or s == "1":
        return "LOW"
    return "MEDIUM"


def _format_event_time(event: dict) -> str:
    """Format event time as 'Mon, Apr 7 at 8:30am ET'."""
    try:
        raw_time = event.get("time", "")
        if raw_time and raw_time != "0000-00-00":
            dt = datetime.datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            # Convert UTC to ET (approximate: UTC-4 for EDT)
            try:
                from zoneinfo import ZoneInfo
                et = dt.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                et = dt.replace(tzinfo=None) + datetime.timedelta(hours=-4)
            day  = et.strftime("%a, %b %-d")
            hour = et.strftime("%-I:%M%p ET").lower()
            return f"{day} at {hour}"
    except Exception:
        pass

    # Fall back to just the date
    raw_date = event.get("date", "")
    if raw_date:
        try:
            dt = datetime.date.fromisoformat(raw_date)
            return dt.strftime("%a, %b %-d")
        except Exception:
            return raw_date
    return "TBD"


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
    finnhub_api_key: str,
    fred_api_key: str,
    kalshi_markets: list[dict],
) -> str:
    """
    Build and return the weekly economic calendar Discord message.
    Caller sends this to #economic-calendar.
    Zero Claude calls — pure data.
    """
    today     = datetime.date.today()
    # Week starting Monday
    mon       = today + datetime.timedelta(days=(7 - today.weekday()) % 7)
    fri       = mon + datetime.timedelta(days=4)
    from_date = mon.strftime("%Y-%m-%d")
    to_date   = fri.strftime("%Y-%m-%d")
    date_range = f"{mon.strftime('%b %-d')}–{fri.strftime('%b %-d, %Y')}"

    # Fetch in parallel
    import asyncio
    fred = _get_fred(fred_api_key)
    events_task = _fetch_finnhub_calendar(finnhub_api_key, from_date, to_date)
    fred_task   = fred.get_all()
    events, fred_data = await asyncio.gather(events_task, fred_task)

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

async def get_daily_calendar_alert(finnhub_api_key: str) -> str | None:
    """
    Check today's Finnhub calendar. If there are HIGH impact events, return a
    one-line alert string for #morning-brief. Returns None if nothing significant.
    Zero Claude calls.
    """
    if not finnhub_api_key:
        return None

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")

    events = await _fetch_finnhub_calendar(finnhub_api_key, today_str, today_str)
    high   = [e for e in events if _impact_label(e.get("impact")) == "HIGH"]

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
