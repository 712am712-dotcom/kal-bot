"""
supabase_logger.py — Drop-in replacement for discord_notifier.py

Routes all Kal output to Supabase instead of Discord:
  - Intelligence signals  → signals table
  - Operational events    → agent_logs table

All functions are async and match the original discord_notifier signatures
so no call sites need to change — only the import alias.
"""
from __future__ import annotations

import json
import logging
import datetime
from typing import Any

from supabase import create_client, Client

from config import settings

log = logging.getLogger(__name__)

# ── Supabase client (singleton) ───────────────────────────────────────────────

_client: Client | None = None

def _sb() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
    return _client


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def _signal(
    brand: str,
    topic: str,
    hook: str = "",
    score: int = 5,
    why_now: str = "",
    source: str = "",
) -> None:
    """Insert a row into the signals table."""
    try:
        _sb().table("signals").insert({
            "brand":   brand,
            "topic":   topic[:500],
            "hook":    hook[:300] if hook else None,
            "score":   max(0, min(10, score)),
            "why_now": why_now[:2000] if why_now else None,
            "source":  source,
        }).execute()
    except Exception as exc:
        log.warning("supabase_signal_failed source=%s error=%s", source, exc)


async def _log(event: str, detail: Any = None, service: str = "kal") -> None:
    """Insert a row into agent_logs."""
    try:
        payload: dict = {}
        if detail is not None:
            if isinstance(detail, dict):
                payload = detail
            else:
                payload = {"message": str(detail)[:2000]}
        _sb().table("agent_logs").insert({
            "service": service,
            "event":   event,
            "detail":  payload or None,
        }).execute()
    except Exception as exc:
        log.warning("supabase_log_failed event=%s error=%s", event, exc)


# ── Signal-generating functions ───────────────────────────────────────────────

async def notify_ai_decision(
    ticker: str = "",
    title: str = "",
    recommendation: str = "",
    edge: float = 0.0,
    confidence: float = 0.0,
    kalshi_price: float = 0.0,
    claude_prob: float = 0.0,
    reasoning: str = "",
    mode: str = "",
    expected_profit: float = 0.0,
    tradeable_score: float = 0.0,
    volume: float = 0.0,
    coin: str = "",
    timeframe: str = "",
    live_price: float = 0.0,
    strike_price: float = 0.0,
    direction: str = "",
    change_24h: float = 0.0,
    **_: Any,
) -> None:
    score = min(10, max(0, int(round(confidence * 10))))
    topic = title or ticker
    hook = f"{recommendation} | edge={edge:.2f} | conf={confidence:.0%}"
    why_now = reasoning[:2000] if reasoning else f"{recommendation} on {ticker}"
    await _signal("KAL", topic, hook=hook, score=score, why_now=why_now, source="kalshi")
    await _log("ai_decision", {
        "ticker": ticker, "recommendation": recommendation,
        "edge": edge, "confidence": confidence, "mode": mode,
    })


async def notify_newsletter_signal(
    from_name: str = "",
    focus: str = "",
    subject: str = "",
    thesis: str = "",
    trade_ideas: list | None = None,
    tickers: list | None = None,
    signal_score: int = 5,
    urgency: str = "",
    one_liner: str = "",
    **_: Any,
) -> None:
    topic = subject or focus or from_name
    hook = one_liner[:300] if one_liner else subject[:300]
    why_now = thesis[:2000] if thesis else ""
    await _signal("MFD", topic, hook=hook, score=signal_score, why_now=why_now, source="newsletter")


async def notify_rss_intel(post: dict | None = None, **_: Any) -> None:
    if not post:
        return
    topic = post.get("title") or post.get("headline") or "RSS Signal"
    hook = post.get("summary", "")[:300]
    score = int(post.get("score", 5))
    why_now = post.get("why_now") or post.get("body", "")[:500]
    await _signal("MFD", topic, hook=hook, score=score, why_now=why_now, source="rss")


async def notify_news_thesis(
    thesis: str = "",
    markets: list | None = None,
    headline: str = "",
    **_: Any,
) -> None:
    topic = headline or thesis[:100]
    await _signal("KAL", topic, hook=thesis[:300], score=7,
                  why_now=thesis, source="news_intelligence")


async def notify_breaking_news(
    article: dict | None = None,
    markets: list | None = None,
    **_: Any,
) -> None:
    if not article:
        return
    topic = article.get("headline") or article.get("title", "Breaking news")
    hook = article.get("summary", "")[:300]
    body = article.get("body") or article.get("content", "")
    await _signal("KAL", topic, hook=hook, score=8, why_now=body[:500], source="breaking_news")


async def notify_breaking_alert(
    post: dict | str | None = None,
    source: str = "",
    **_: Any,
) -> None:
    if isinstance(post, dict):
        topic = post.get("headline") or post.get("title", "Breaking alert")
        hook = post.get("summary", "")[:300]
        why_now = post.get("body", "")[:500]
    else:
        topic = str(post)[:200] if post else "Breaking alert"
        hook = ""
        why_now = ""
    await _signal("KAL", topic, hook=hook, score=8, why_now=why_now, source="breaking")


async def notify_intelligence_high_conviction(
    ticker: str = "",
    title: str = "",
    signal_type: str = "",
    detail: str = "",
    confidence: float = 0.0,
    **_: Any,
) -> None:
    score = min(10, max(0, int(round(confidence * 10))))
    await _signal("KAL", title or ticker, hook=signal_type[:200],
                  score=score, why_now=detail[:500], source="market_intelligence")


async def notify_intelligence_price_move(
    ticker: str = "", pct: float = 0.0, direction: str = "", **_: Any,
) -> None:
    topic = f"{ticker} price {direction} {pct:.1f}%"
    await _signal("KAL", topic, score=6, source="market_intelligence")


async def notify_intelligence_volume_spike(
    ticker: str = "", volume: float = 0.0, **_: Any,
) -> None:
    topic = f"{ticker} volume spike ${volume:,.0f}"
    await _signal("KAL", topic, score=6, source="market_intelligence")


async def notify_intelligence_new_market(
    ticker: str = "", title: str = "", **_: Any,
) -> None:
    await _signal("KAL", title or ticker, score=5, source="market_intelligence")


async def notify_intelligence_summary(summary: str = "", **_: Any) -> None:
    await _log("intelligence_summary", {"summary": summary[:1000]})


# ── Content job functions ─────────────────────────────────────────────────────

async def notify_todays_focus(focus: str = "", **_: Any) -> None:
    """Trade Today brief → logged to agent_logs."""
    await _log("trade_today_posted", {"length": len(focus)})


async def notify_mfd_newsletter_draft(
    subject_a: str = "",
    subject_b: str = "",
    subject_c: str = "",
    preview_text: str = "",
    html: str = "",
    plain_text: str = "",
    **_: Any,
) -> None:
    """MFD newsletter draft → logged to agent_logs."""
    await _log("mfd_newsletter_draft_created", {"subject_a": subject_a})


async def notify_morning_brief(brief: str = "", **_: Any) -> None:
    await _log("morning_brief", {"length": len(brief), "preview": brief[:200]})


# ── Operational / status functions (→ agent_logs) ────────────────────────────

async def notify_bot_started(mode: str = "", demo: bool = True, **_: Any) -> None:
    await _log("bot_started", {"mode": mode, "demo": demo})


async def notify_bot_stopped(**_: Any) -> None:
    await _log("bot_stopped")


async def notify_error(
    context: str = "",
    error: str = "",
    critical: bool = False,
    **_: Any,
) -> None:
    await _log("error", {"context": context, "error": error[:500], "critical": critical})


async def notify_credits_exhausted(**_: Any) -> None:
    await _log("credits_exhausted")


async def notify_credits_restored(**_: Any) -> None:
    await _log("credits_restored")


async def notify_cost_warning(
    daily_cost: float = 0.0,
    limit: float = 0.0,
    **_: Any,
) -> None:
    await _log("cost_warning", {"daily_cost": daily_cost, "limit": limit})


async def notify_cost_limit_hit(
    daily_cost: float = 0.0,
    limit: float = 0.0,
    **_: Any,
) -> None:
    await _log("cost_limit_hit", {"daily_cost": daily_cost, "limit": limit})


async def notify_daily_cost_report(
    total_cost: float = 0.0,
    model_costs: dict | None = None,
    **_: Any,
) -> None:
    await _log("daily_cost_report", {"total_cost": total_cost, "model_costs": model_costs})


async def notify_trade_placed(
    ticker: str = "",
    side: str = "",
    contracts: int = 0,
    price_cents: int = 0,
    amount_dollars: float = 0.0,
    order_id: str = "",
    **_: Any,
) -> None:
    await _log("trade_placed", {
        "ticker": ticker, "side": side, "contracts": contracts,
        "price_cents": price_cents, "amount_dollars": amount_dollars,
        "order_id": order_id,
    })


async def notify_trade_resolved(
    ticker: str = "",
    side: str = "",
    resolution: str = "",
    pnl_dollars: float = 0.0,
    contracts: int = 0,
    **_: Any,
) -> None:
    await _log("trade_resolved", {
        "ticker": ticker, "side": side,
        "resolution": resolution, "pnl_dollars": pnl_dollars,
    })


async def notify_trade_failed(
    ticker: str = "",
    reason: str = "",
    **_: Any,
) -> None:
    await _log("trade_failed", {"ticker": ticker, "reason": reason[:300]})


async def notify_position_update(positions: list | None = None, **_: Any) -> None:
    await _log("position_update", {"count": len(positions) if positions else 0})


async def notify_research_complete(
    markets_analyzed: int = 0,
    decisions: list | None = None,
    duration_seconds: float = 0.0,
    **_: Any,
) -> None:
    await _log("research_complete", {
        "markets_analyzed": markets_analyzed,
        "duration_seconds": round(duration_seconds, 1),
    })


async def notify_periodic_summary(
    period_hours: float = 6.0,
    stats: dict | None = None,
    **_: Any,
) -> None:
    await _log("periodic_summary", stats or {})


async def notify_daily_summary(**_: Any) -> None:
    await _log("daily_summary")


async def notify_daily_performance(
    today_stats: dict | None = None,
    all_time_stats: dict | None = None,
    xlsx_bytes: bytes | None = None,
    demo: bool = True,
    **_: Any,
) -> None:
    await _log("daily_performance", {
        "today": today_stats or {},
        "all_time": {k: v for k, v in (all_time_stats or {}).items() if k != "xlsx"},
        "demo": demo,
    })


async def notify_credit_alert(message: str = "", **_: Any) -> None:
    await _log("credit_alert", {"message": message[:300]})


async def notify_market_pulse(pulse: dict | None = None, **_: Any) -> None:
    await _log("market_pulse", pulse or {})


async def notify_ta_hourly_summary(summary: dict | None = None, **_: Any) -> None:
    await _log("ta_hourly_summary", summary or {})


async def notify_connection_issue(message: str = "", **_: Any) -> None:
    await _log("connection_issue", {"message": message[:300]})


async def notify_economic_calendar(
    events: list | None = None,
    markets: list | None = None,
    **_: Any,
) -> None:
    await _log("economic_calendar", {"event_count": len(events or [])})


async def notify_economic_calendar_weekly(content: str = "", **_: Any) -> None:
    await _log("economic_calendar_weekly", {"preview": content[:300]})


async def notify_daily_calendar_alert(alert: str = "", **_: Any) -> None:
    await _log("daily_calendar_alert", {"alert": alert[:500]})


async def notify_day_before_macro_alert(alert_text: str = "", **_: Any) -> None:
    await _log("day_before_macro_alert", {"alert": alert_text[:500]})


async def notify_market_open(content: str = "", **_: Any) -> None:
    await _log("market_open", {"preview": content[:300]})


async def notify_market_close(content: str = "", **_: Any) -> None:
    await _log("market_close", {"preview": content[:300]})


async def notify_weekly_analysis(content: str = "", **_: Any) -> None:
    await _log("weekly_analysis", {"preview": content[:300]})


async def notify_todays_focus_brief(content: str = "", **_: Any) -> None:
    await _log("todays_focus_brief", {"preview": content[:300]})


async def notify_daily_briefing(content: str = "", **_: Any) -> None:
    await _log("daily_briefing", {"preview": content[:300]})


async def notify_system_log(message: str = "", **_: Any) -> None:
    await _log("system_log", {"message": message[:500]})


async def notify_owner_response(message: str = "", **_: Any) -> None:
    await _log("owner_response", {"message": message[:500]})


async def notify_daily_qa_report(summary: str = "", **_: Any) -> None:
    await _log("daily_qa_report", {"summary": summary[:500]})


# ── No-ops (previously Discord-only housekeeping) ────────────────────────────

async def send_channel_guide(**_: Any) -> None:
    """No-op — Discord channel guide is deprecated."""


async def notify_connection_status(**_: Any) -> None:
    pass
