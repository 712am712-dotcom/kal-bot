"""
content_jobs.py — Content job generation for the MFD/AE content pipeline.

Creates content_jobs rows in Supabase that Scribe + Content Engine pick up.

Pipeline:
  1. generate_content_job(template, brand) pulls recent signals
  2. Inserts a content_jobs row with status=pending and raw_signal string
  3. Scribe (Node.js) polls, claims it, generates a script → status=scripted
  4. Content Engine (Node.js) polls, renders MP4 → status=rendered

Supported templates:
  mfd-market-focus       — overnight + premarket data
  mfd-market-reflection  — close data + biggest movers
  mfd-news-headline      — current event data
  mfd-educational        — topic bank, rotates
  ae-signal              — AI/tech news signals

Expose via: POST /api/generate-content
  Body: {"template": "mfd-market-focus", "brand": "mfd"}
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)


# ── Template → brand + renderer defaults ─────────────────────────────────────

_TEMPLATE_DEFAULTS: dict[str, dict] = {
    "mfd-market-focus":      {"brand": "mfd", "renderer": "remotion", "format": "short-form-video"},
    "mfd-market-reflection": {"brand": "mfd", "renderer": "remotion", "format": "short-form-video"},
    "mfd-news-headline":     {"brand": "mfd", "renderer": "remotion", "format": "short-form-video"},
    "mfd-educational":       {"brand": "mfd", "renderer": "remotion", "format": "short-form-video"},
    "ae-signal":             {"brand": "ae",  "renderer": "remotion", "format": "short-form-video"},
}


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate_content_job(template: str, brand: str) -> str | None:
    """
    Pull relevant signals from Supabase, build a raw_signal string,
    and insert a pending content_jobs row.

    Returns the new job UUID on success, None on failure.

    The raw_signal is a plain-English string that Scribe's LLM uses
    as the creative brief. Keep it concise and specific.
    """
    template = template.strip().lower()
    brand    = brand.strip().lower()

    defaults = _TEMPLATE_DEFAULTS.get(template, {
        "brand": brand, "renderer": "remotion", "format": "short-form-video"
    })
    effective_brand    = brand or defaults["brand"]
    effective_renderer = defaults["renderer"]
    effective_format   = defaults["format"]

    # 1. Pull signals from the last 24h
    signals = await _fetch_recent_signals(brand=effective_brand.upper(), hours=24)

    # 2. Build a template-specific raw_signal string
    raw_signal = _build_raw_signal(template, effective_brand, signals)

    if not raw_signal.strip():
        log.warning("[content-jobs] no signal material for template=%s brand=%s", template, brand)
        raw_signal = f"Generate a {template} post for {effective_brand.upper()}."

    # 3. Insert content_jobs row
    job_id = await _insert_content_job(
        brand=effective_brand,
        template=template,
        renderer=effective_renderer,
        fmt=effective_format,
        raw_signal=raw_signal,
    )

    if job_id:
        log.info(
            "[content-jobs] created job=%s template=%s brand=%s signal_len=%d",
            job_id[:8], template, effective_brand, len(raw_signal),
        )

    return job_id


# ── Signal fetcher ────────────────────────────────────────────────────────────

async def _fetch_recent_signals(brand: str = "", hours: int = 24) -> list[dict]:
    """Fetch recent signals from Supabase signals table."""
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return []

        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        ).isoformat() + "Z"

        params = (
            f"?order=score.desc"
            f"&created_at=gte.{cutoff}"
            f"&limit=20"
        )
        if brand:
            params += f"&brand=eq.{brand}"

        url = f"{settings.supabase_url}/rest/v1/signals{params}"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=headers)
        if r.status_code == 200:
            return r.json() or []
        log.debug("[content-jobs] signals fetch %d: %s", r.status_code, r.text[:100])
    except Exception as exc:
        log.warning("[content-jobs] signals fetch failed: %s", exc)
    return []


# ── Raw signal builder ────────────────────────────────────────────────────────

def _build_raw_signal(template: str, brand: str, signals: list[dict]) -> str:
    """
    Convert recent signals into a plain-English brief that Scribe's Claude
    can turn into a script. One focused paragraph — specific, not generic.
    """
    if not signals:
        return ""

    # Pick the highest-scoring signal(s) relevant to the template
    top = signals[:5]

    if template == "mfd-market-focus":
        return _market_focus_signal(top)
    elif template == "mfd-market-reflection":
        return _market_reflection_signal(top)
    elif template == "mfd-news-headline":
        return _news_headline_signal(top)
    elif template == "mfd-educational":
        return _educational_signal(top)
    elif template == "ae-signal":
        return _ae_signal(top)
    else:
        # Generic fallback: highest-score signal
        best = top[0]
        return (
            f"{best.get('topic', '')}. "
            f"{best.get('why_now', '')}".strip()
        )


def _market_focus_signal(signals: list[dict]) -> str:
    """mfd-market-focus: what's setting up before the bell."""
    lines = ["Here's what's setting up before the market opens:"]
    for sig in signals[:4]:
        topic   = sig.get("topic") or ""
        why_now = sig.get("why_now") or ""
        hook    = sig.get("hook") or ""
        if topic:
            lines.append(f"• {topic}. {why_now or hook}")
    lines.append(
        "Explain what each move means for everyday investors in plain English. "
        "Lead with the most market-moving development."
    )
    return " ".join(lines)


def _market_reflection_signal(signals: list[dict]) -> str:
    """mfd-market-reflection: end-of-day analysis."""
    price_sigs = [s for s in signals if "%" in s.get("topic", "")]
    other_sigs = [s for s in signals if s not in price_sigs]
    combined = price_sigs[:2] + other_sigs[:2]

    lines = ["Here's what moved today and why it matters:"]
    for sig in combined:
        topic   = sig.get("topic") or ""
        why_now = sig.get("why_now") or ""
        if topic:
            lines.append(f"• {topic}. {why_now}")
    lines.append(
        "Connect each move to how it affects real people — "
        "401k, mortgage rates, savings, gas prices."
    )
    return " ".join(lines)


def _news_headline_signal(signals: list[dict]) -> str:
    """mfd-news-headline: one news story, explained."""
    best = signals[0]
    topic   = best.get("topic") or ""
    why_now = best.get("why_now") or ""
    hook    = best.get("hook") or ""
    return (
        f"The big story right now: {topic}. {why_now or hook} "
        f"Explain this in plain English — what happened, why it matters, "
        f"and what it means for everyday people's money."
    )


def _educational_signal(signals: list[dict]) -> str:
    """mfd-educational: use current events to teach a concept."""
    best = signals[0]
    topic   = best.get("topic") or ""
    why_now = best.get("why_now") or ""
    return (
        f"Use this current event as the hook: {topic}. {why_now} "
        f"Teach the underlying financial concept in plain English. "
        f"Make it relatable to someone who has never studied finance."
    )


def _ae_signal(signals: list[dict]) -> str:
    """ae-signal: AI/tech intelligence brief."""
    ai_sigs = [
        s for s in signals
        if any(k in (s.get("topic") or "").lower()
               for k in ("ai", "model", "openai", "google", "anthropic", "tech",
                         "nvidia", "chip", "llm", "gpt", "gemini", "claude"))
    ]
    use_sigs = ai_sigs[:3] if ai_sigs else signals[:3]

    lines = ["Here's the AI/tech signal to script:"]
    for sig in use_sigs:
        topic   = sig.get("topic") or ""
        why_now = sig.get("why_now") or ""
        hook    = sig.get("hook") or ""
        if topic:
            lines.append(f"• {topic}. {why_now or hook}")
    lines.append(
        "Write for an audience that's technically curious but not a developer. "
        "What is actually changing? Who wins, who loses?"
    )
    return " ".join(lines)


# ── Supabase insert ───────────────────────────────────────────────────────────

async def _insert_content_job(
    brand: str,
    template: str,
    renderer: str,
    fmt: str,
    raw_signal: str,
) -> str | None:
    """Insert a pending content_jobs row. Returns new job UUID or None."""
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            log.warning("[content-jobs] Supabase not configured")
            return None

        url = f"{settings.supabase_url}/rest/v1/content_jobs"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type":  "application/json",
            "Prefer":        "return=representation",
        }
        body = {
            "brand":    brand,
            "template": template,
            "renderer": renderer,
            "format":   fmt,
            "status":   "pending",
            "content":  {
                "raw_signal":   raw_signal,
                "template":     template,
                "brand":        brand,
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            },
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers=headers, json=body)

        if r.status_code in (200, 201):
            rows = r.json()
            if isinstance(rows, list) and rows:
                return rows[0].get("id")
            if isinstance(rows, dict):
                return rows.get("id")
            return "created"

        log.warning("[content-jobs] insert %d: %s", r.status_code, r.text[:200])
        return None

    except Exception as exc:
        log.warning("[content-jobs] insert failed: %s", exc)
        return None
