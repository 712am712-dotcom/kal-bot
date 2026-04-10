"""
mfd_newsletter.py — Daily MFD (Markets For Dummies) newsletter generation.

Runs at 6:05am ET daily after the morning brief and Trade Today.

Content sources (Supabase bot_communications, last 12h):
  - morning-brief      → channel = morning-brief
  - the-trade-today    → channel = the-trade-today
  - breaking news      → channel = breaking  (top 3)
  - RSS / market intel → channel = market-pulse, message_type = rss_intel (top 5)

Pipeline:
  1. Fetch recent content from Supabase
  2. Claude generates full newsletter HTML + 3 subject lines + preview text
  3. POST to Beehiiv as draft
  4. Post draft summary + subjects to #mfd-newsletter-queue on Discord
  5. Log dispatch to bot_communications (message_type=mfd-newsletter)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging

import httpx

from config import settings

log = logging.getLogger(__name__)

BEEHIIV_API_BASE = "https://api.beehiiv.com/v2"

# ── Claude prompts ─────────────────────────────────────────────────────────────

_NEWSLETTER_SYSTEM = """You are Kal, the AI behind the "Markets For Dummies" daily newsletter.

Voice rules — never break these:
1. No jargon without an immediate plain-English definition on the same line.
   Bad:  "The Fed is hawkish."
   Good: "The Fed signaled it may raise rates — the price banks charge to borrow money."
2. Every significant number gets a "so what" in parentheses.
   Bad:  "The S&P 500 fell 0.4%."
   Good: "The S&P 500 (a basket tracking America's 500 biggest companies) fell 0.4% — that's roughly $200 on a $50,000 portfolio."
3. Write for someone who reads the news but skips the financial pages.
4. Short paragraphs — max 3 sentences each. White space is your friend.
5. One story per section. Don't cram multiple events together.
6. No weasel words. Don't write "could", "may potentially" — pick a side or skip the claim.
7. Always answer implicitly: "Why does this affect me?"

Newsletter structure (use exactly these section headers):
  THE TRADE TODAY     — what to watch or think about today (from trade_today input)
  WHAT HAPPENED       — top 2-3 news items in plain English
  THE NUMBER          — one stat, fully explained with context
  WHAT TO WATCH       — 1-2 forward-looking items for the next 24-48h
"""

_NEWSLETTER_USER_TMPL = """Today is {date}. Here is the raw content to work from:

=== THE TRADE TODAY ===
{trade_today}

=== MORNING BRIEF ===
{morning_brief}

=== BREAKING NEWS (top items) ===
{breaking_news}

=== RSS / MARKET INTEL ===
{rss_intel}

---
Generate a complete MFD newsletter for today. Return ONLY valid JSON, no markdown fences, with these exact keys:

{{
  "html": "<full HTML newsletter body — inline styles only, Beehiiv-compatible>",
  "subject_a": "<provocative fact angle — lead with a number or surprising stat, max 60 chars>",
  "subject_b": "<contrarian angle — challenge conventional wisdom, max 60 chars>",
  "subject_c": "<plain event description — what happened, max 60 chars>",
  "preview_text": "<one-sentence teaser, does not repeat the subject line, max 100 chars>"
}}

HTML requirements:
- Outer wrapper: <div style="max-width:600px;margin:0 auto;padding:0 20px;font-family:Georgia,serif;font-size:16px;line-height:1.7;color:#1a1a1a;">
- Section headers: <p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">SECTION NAME</p>
- Section dividers: <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
- Highlight boxes (for The Trade Today): <div style="background:#f9fafb;border-left:3px solid #F1C40F;padding:14px 18px;margin:16px 0;">
- Body paragraphs: <p style="margin:0 0 14px;">
- Bold key terms: <strong>
- Footer: <p style="font-size:13px;color:#9ca3af;margin-top:36px;border-top:1px solid #e5e7eb;padding-top:16px;">Markets For Dummies — plain English, every trading day. Reply to this email with questions.</p>

If a section has no content, omit it entirely (don't write placeholder text).
"""

# ── Supabase content fetch ─────────────────────────────────────────────────────

async def _fetch_channel(
    channel: str,
    hours: int = 12,
    limit: int = 3,
    message_type: str | None = None,
) -> list[str]:
    """
    Fetch recent bot_communications entries for a channel.
    Returns list of content strings (newest first).
    """
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return []
        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        ).isoformat() + "Z"
        params = (
            f"?channel=eq.{channel}"
            f"&timestamp=gte.{cutoff}"
            f"&order=timestamp.desc"
            f"&limit={limit}"
        )
        if message_type:
            params += f"&message_type=eq.{message_type}"
        url = f"{settings.supabase_url}/rest/v1/bot_communications{params}"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=headers)
        if r.status_code == 200:
            return [row.get("content", "") for row in r.json() if row.get("content")]
        log.debug("[mfd-newsletter] supabase %s %s: %s", channel, r.status_code, r.text[:100])
    except Exception as exc:
        log.warning("[mfd-newsletter] supabase fetch %s failed: %s", channel, exc)
    return []


async def _gather_content() -> dict[str, str]:
    """Fetch all content sources in parallel. Returns dict keyed for Claude prompt."""
    trade_today_rows, brief_rows, breaking_rows, rss_rows = await asyncio.gather(
        _fetch_channel("the-trade-today", hours=12, limit=1),
        _fetch_channel("morning-brief",   hours=12, limit=1),
        _fetch_channel("breaking",         hours=12, limit=3),
        _fetch_channel("market-pulse",     hours=12, limit=5, message_type="rss_intel"),
    )
    return {
        "trade_today":   "\n\n".join(trade_today_rows) or "(none today)",
        "morning_brief": "\n\n".join(brief_rows)       or "(none today)",
        "breaking_news": "\n\n".join(f"• {x}" for x in breaking_rows) or "(none today)",
        "rss_intel":     "\n\n".join(f"• {x}" for x in rss_rows)      or "(none today)",
    }


def _has_content(sources: dict[str, str]) -> bool:
    """True if there is at least one real content source to work from."""
    return any(v != "(none today)" for v in sources.values())


# ── Claude newsletter generation ───────────────────────────────────────────────

async def generate_mfd_newsletter(
    api_key: str,
    model: str = "claude-opus-4-6",
) -> dict | None:
    """
    Generate a full MFD newsletter draft.

    Returns dict: {html, subject_a, subject_b, subject_c, preview_text}
    Returns None if content is insufficient or Claude fails.
    """
    sources = await _gather_content()

    if not _has_content(sources):
        log.info("[mfd-newsletter] no content in last 12h — skipping")
        return None

    # Windows strftime doesn't support %-d; use a small workaround
    today = datetime.date.today()
    today_str = today.strftime("%B") + " " + str(today.day) + ", " + str(today.year)

    user_prompt = _NEWSLETTER_USER_TMPL.format(
        date=today_str,
        trade_today=sources["trade_today"][:1500],
        morning_brief=sources["morning_brief"][:2000],
        breaking_news=sources["breaking_news"][:1500],
        rss_intel=sources["rss_intel"][:1000],
    )

    try:
        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 4096,
                    "system":     _NEWSLETTER_SYSTEM,
                    "messages":   [{"role": "user", "content": user_prompt}],
                },
            )
        if r.status_code != 200:
            log.warning("[mfd-newsletter] Claude %s: %s", r.status_code, r.text[:300])
            return None

        raw = r.json()["content"][0]["text"].strip()

        # Strip markdown fences if Claude wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()

        result = json.loads(raw)

        required = {"html", "subject_a", "subject_b", "subject_c", "preview_text"}
        missing = required - result.keys()
        if missing:
            log.warning("[mfd-newsletter] missing keys from Claude: %s", missing)
            return None

        return result

    except json.JSONDecodeError as exc:
        log.warning("[mfd-newsletter] JSON parse failed: %s", exc)
        return None
    except Exception as exc:
        log.warning("[mfd-newsletter] Claude call failed: %s", exc)
        return None


# ── Beehiiv API ────────────────────────────────────────────────────────────────

async def post_to_beehiiv(
    html: str,
    subject: str,
    preview_text: str,
    api_key: str,
    publication_id: str,
) -> str | None:
    """
    POST a draft newsletter to Beehiiv.
    Returns the Beehiiv post ID on success, None on failure.
    """
    try:
        url = f"{BEEHIIV_API_BASE}/publications/{publication_id}/posts"
        payload = {
            "title":          subject,
            "subtitle":       preview_text,
            "status":         "draft",
            "body_content":   html,
            "content_type":   "html",
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        if r.status_code in (200, 201):
            data = r.json()
            post_id = data.get("data", {}).get("id") or data.get("id", "unknown")
            log.info("[mfd-newsletter] Beehiiv draft created: %s", post_id)
            return str(post_id)
        log.warning("[mfd-newsletter] Beehiiv %s: %s", r.status_code, r.text[:400])
        return None
    except Exception as exc:
        log.warning("[mfd-newsletter] Beehiiv post failed: %s", exc)
        return None


# ── Supabase dispatch log ──────────────────────────────────────────────────────

async def log_newsletter_dispatch(subject: str, beehiiv_id: str | None) -> None:
    """Log the newsletter draft dispatch to bot_communications."""
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return
        content = f"MFD Newsletter draft — subject: {subject}"
        if beehiiv_id:
            content += f" | beehiiv_id: {beehiiv_id}"
        url = f"{settings.supabase_url}/rest/v1/bot_communications"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        }
        body = {
            "channel":         "mfd-published",
            "message_type":    "mfd-newsletter",
            "content":         content,
            "delivery_method": "beehiiv",
            "status":          "delivered" if beehiiv_id else "failed",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers=headers, json=body)
        if r.status_code not in (200, 201):
            log.debug("[mfd-newsletter] supabase log %s: %s", r.status_code, r.text[:100])
    except Exception as exc:
        log.debug("[mfd-newsletter] supabase log error: %s", exc)
