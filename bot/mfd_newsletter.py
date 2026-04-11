"""
mfd_newsletter.py — Daily MFD (Markets For Dummies) newsletter generation.

Runs at 6:05am ET daily after the morning brief and Trade Today.

Content sources (Supabase bot_communications, last 24h):
  - the-trade-today  → verbatim, never paraphrased
  - morning-brief    → opener + macro context
  - breaking         → top stories + More Stories pool
  - market-pulse     → rss_intel items for More Stories pool
  - market-open/close→ numbers for Before The Bell table

Locked template order (enforced in system prompt):
  1. Header: MARKETS FOR DUMMIES | Date
  2. Opener (2-3 sentences, macro mood)
  3. BEFORE THE BELL (numbers table)
  4. WHAT'S GOING ON (top 2-3 stories)
  5. EARNINGS (beat/miss + 1 sentence)
  6. MORE STORIES (8-12 scannable one-liners)
  7. THE TRADE TODAY (verbatim + 1 italic line)
  8. ONE THING TO WATCH
  9. Sign-off + tagline
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re

import httpx

from config import settings

log = logging.getLogger(__name__)

BEEHIIV_API_BASE = "https://api.beehiiv.com/v2"


# ── HTML → plain text ─────────────────────────────────────────────────────────

def html_to_plain(html: str) -> str:
    """Convert newsletter HTML to readable plain text for Discord preview."""
    text = re.sub(
        r'<p[^>]*style="[^"]*text-transform:\s*uppercase[^"]*"[^>]*>(.*?)</p>',
        lambda m: f"\n\n── {m.group(1).strip().upper()} ──\n",
        html, flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr[^>]*>', '\n' + '─' * 40 + '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'_\1_', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'_\1_', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'</p>|</div>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Prompts ───────────────────────────────────────────────────────────────────

_NEWSLETTER_SYSTEM = """You are generating Markets for Dummies — a daily financial newsletter for Gen Z and young people who want to understand markets but were never taught. You must follow the template EXACTLY. The template is the law — do not add, remove, or rename any sections. Be clear, direct, confident. Never condescending. Never use jargon without plain English follow-up in the same sentence. Short sentences. Mobile-first.

VOICE RULES — break these and the output is rejected:
1. No jargon without immediate plain-English on the same line.
   Wrong: "The Fed is hawkish."
   Right: "The Fed signaled it may raise rates — the price banks charge to borrow money."
2. Every significant number gets a "so what."
   Wrong: "The S&P 500 fell 0.4%."
   Right: "The S&P 500 (tracks America's 500 biggest companies) fell 0.4% — that's about $200 on a $50,000 portfolio."
3. Answer implicitly: "Why does this affect me?"
4. Short paragraphs. 2-3 sentences max. White space is your friend.
5. Never write "could", "may potentially" — pick a side or skip the claim.
6. On macro release days (CPI, PCE, GDP, NFP, FOMC, PPI, Retail Sales): this event MUST be the first story in WHAT'S GOING ON. State what it printed, vs estimate, vs previous, then what it means.

LOCKED TEMPLATE — follow this EXACT order, EXACT section names:

SECTION 1: Header
<div style="font-size:11px;font-weight:bold;letter-spacing:0.12em;text-transform:uppercase;color:#888888;margin:0 0 4px;">MARKETS FOR DUMMIES</div>
<div style="font-size:28px;font-weight:bold;color:#1a1a1a;margin:0 0 8px;">[Date like: Thursday, April 10]</div>
<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">

SECTION 2: Opener
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">GOOD MORNING</p>
2-3 sentence macro mood. No jargon. Tell the reader what kind of day it is for markets.

SECTION 3: BEFORE THE BELL
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">BEFORE THE BELL</p>
Numbers table — MANDATORY. Use data from the MARKET DATA section of input. Format:
<table style="width:100%;border-collapse:collapse;font-size:14px;">
  <tr style="border-bottom:1px solid #e5e7eb;color:#888888;font-size:11px;font-weight:bold;letter-spacing:0.05em;">
    <td style="padding:6px 0;">INDEX</td><td style="padding:6px 0;text-align:right;">LAST</td><td style="padding:6px 0;text-align:right;">DAY</td><td style="padding:6px 0;text-align:right;">YTD</td>
  </tr>
  [S&P 500 row] [Dow row] [Nasdaq row] [Russell 2000 row]
  <tr><td colspan="4" style="padding:4px 0;border-top:1px solid #e5e7eb;"></td></tr>
  [Bitcoin row] [Ethereum row] [Solana row]
  <tr><td colspan="4" style="padding:4px 0;border-top:1px solid #e5e7eb;"></td></tr>
  [Gold row] [Oil WTI row] [10Y Treasury row]
</table>
Row format: <tr><td style="padding:6px 0;">[Name]</td><td style="padding:6px 0;text-align:right;">[price]</td><td style="padding:6px 0;text-align:right;color:[green/red];">[day%]</td><td style="padding:6px 0;text-align:right;color:[green/red];">[ytd% or N/A]</td></tr>
Green = #00C076, Red = #EF4444. Use N/A if data missing. Never invent numbers.
One italic context sentence below the table (e.g. "Markets are digesting today's CPI print.").

SECTION 4: WHAT'S GOING ON
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">WHAT'S GOING ON</p>
Top 2-3 stories. Each story: bold headline, then 2-3 sentence plain-English explanation.
If today has a macro release (CPI/PCE/GDP/NFP/FOMC/PPI): IT MUST BE STORY #1. State the number, the estimate, the previous, what it means.

SECTION 5: EARNINGS
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">EARNINGS</p>
Only include if earnings data exists. Beat/miss + 1 plain-English sentence per company. If no earnings data, omit this section entirely.

SECTION 6: MORE STORIES
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">MORE STORIES</p>
8-12 scannable one-liners from the breaking news and RSS intel provided. NO duplicates with WHAT'S GOING ON.
Sort by relevance: macro data first → equities → tech/AI → everything else.
Each line: <p style="margin:0 0 10px;">▸ <strong>[Plain English headline]</strong> — [one plain English sentence of context]</p>
Never invent stories. Only use items from the provided MORE STORIES POOL.

SECTION 7: THE TRADE TODAY
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">🎯 THE TRADE TODAY</p>
<div style="background:#f9fafb;border-left:3px solid #F1C40F;padding:14px 18px;margin:16px 0;">
COPY THE TRADE TODAY TEXT VERBATIM. Do not paraphrase. Do not summarize. Copy it word for word.
</div>
One italic plain-English sentence after the box — nothing more. E.g. <em>This is the one number to watch before you open any position today.</em>

SECTION 8: ONE THING TO WATCH
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">ONE THING TO WATCH</p>
One forward-looking sentence for the next 24-48 hours. Specific event or data point.

SECTION 9: Sign-off
<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
<p style="margin:0 0 4px;font-weight:bold;">See you tomorrow.</p>
<p style="margin:0;color:#888888;font-size:14px;">— Markets for Dummies</p>
<p style="font-size:13px;color:#9ca3af;margin-top:24px;font-style:italic;">Wall Street, translated.</p>
<p style="font-size:12px;color:#9ca3af;margin-top:16px;">Reply to this email with questions or feedback.</p>"""


_NEWSLETTER_USER_TMPL = """Today is {date}. Generate the complete MFD newsletter using ONLY the data provided below.

=== THE TRADE TODAY (COPY VERBATIM — do not change a single word) ===
{trade_today}

=== MORNING BRIEF (use for opener + WHAT'S GOING ON stories) ===
{morning_brief}

=== MARKET DATA ({market_data_label}) ===
{market_data}

=== BREAKING NEWS — TOP STORIES (use for WHAT'S GOING ON) ===
{breaking_top}

=== MORE STORIES POOL (pick 8-12 for MORE STORIES section — exclude any already in WHAT'S GOING ON) ===
{more_stories_pool}

---
Return ONLY valid JSON — no markdown fences, no commentary. Use these exact keys:

{{
  "html": "<complete HTML newsletter — outer wrapper + all 9 sections in locked order — inline styles only>",
  "subject_a": "<provocative fact — lead with a number or surprising stat, max 60 chars>",
  "subject_b": "<contrarian angle — challenge conventional wisdom, max 60 chars>",
  "subject_c": "<plain event — what happened, max 60 chars>",
  "preview_text": "<one-sentence teaser, does not repeat subject line, max 100 chars>"
}}

Outer wrapper: <div style="max-width:600px;margin:0 auto;padding:0 20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:16px;line-height:1.7;color:#1a1a1a;">

CRITICAL RULES:
- The Trade Today: copy the text VERBATIM from the === THE TRADE TODAY === section above. Not one word changed.
- More Stories: use ONLY items from the MORE STORIES POOL. 8 minimum, 12 maximum.
- Numbers table: use ONLY numbers from the MARKET DATA section. Write N/A for any missing value. Never invent a price.
- Macro releases (CPI/PCE/GDP/NFP/FOMC/PPI/Retail Sales): if present in any input section, they MUST be Story #1 in WHAT'S GOING ON with the actual print, estimate, previous, and plain-English meaning.
- Do not add, remove, or rename any sections. Follow the locked template exactly."""


# ── Supabase content fetch ────────────────────────────────────────────────────

async def _fetch_channel(
    channel: str,
    hours: int = 24,
    limit: int = 20,
    message_type: str | None = None,
) -> list[str]:
    """Fetch recent bot_communications entries. Returns list of content strings."""
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
    """
    Fetch all content sources in parallel.
    Returns dict with keys:
      trade_today, morning_brief, market_data, market_data_label,
      breaking_top (top 3 for WHAT'S GOING ON),
      more_stories_pool (all remaining items for MORE STORIES)
    """
    # On weekends, look back 72h so we pick up Friday's market-close data.
    today = datetime.date.today()
    is_weekend = today.weekday() >= 5  # 5=Saturday, 6=Sunday
    market_lookback_hours = 72 if is_weekend else 24

    (
        trade_today_rows,
        brief_rows,
        market_open_rows,
        market_close_rows,
        market_pulse_rows,
        breaking_rows,
        rss_rows,
    ) = await asyncio.gather(
        _fetch_channel("the-trade-today", hours=24,                   limit=1),
        _fetch_channel("morning-brief",   hours=24,                   limit=1),
        _fetch_channel("market-open",     hours=market_lookback_hours, limit=1),
        _fetch_channel("market-close",    hours=market_lookback_hours, limit=1),
        _fetch_channel("market-pulse",    hours=market_lookback_hours, limit=1),
        _fetch_channel("breaking",        hours=24,                   limit=20),
        _fetch_channel("market-pulse",    hours=24, limit=20, message_type="rss_intel"),
    )

    # Market data: prefer market-close (has end-of-day), then market-open, then market-pulse
    market_data_rows = market_close_rows or market_open_rows or market_pulse_rows
    market_data_text = "\n\n".join(market_data_rows[:1]) if market_data_rows else "(no market data available)"

    # Compute the label for the numbers table header (weekends → last Friday's close)
    if is_weekend:
        days_since_friday = (today.weekday() - 4) % 7  # Saturday=1, Sunday=2
        last_friday = today - datetime.timedelta(days=days_since_friday)
        market_data_label = (
            f"As of {last_friday.strftime('%A, %B')} {last_friday.day} market close"
        )
    else:
        market_data_label = "As of today's market data"

    # Top 3 breaking news items for WHAT'S GOING ON
    top_breaking = breaking_rows[:3]
    breaking_top_text = "\n\n".join(f"• {x}" for x in top_breaking) if top_breaking else "(none today)"

    # More stories pool: remaining breaking items + all RSS intel, deduplicated
    remaining_breaking = breaking_rows[3:]
    all_pool_items = remaining_breaking + rss_rows
    # Deduplicate by first 80 chars
    seen: set[str] = set()
    pool_unique: list[str] = []
    for item in all_pool_items:
        key = item[:80].lower()
        if key not in seen:
            seen.add(key)
            pool_unique.append(item)

    more_stories_pool_text = (
        "\n".join(f"• {x[:300]}" for x in pool_unique[:25])
        if pool_unique
        else "(no additional stories available)"
    )

    return {
        "trade_today":        "\n\n".join(trade_today_rows) or "(no Trade Today today)",
        "morning_brief":      "\n\n".join(brief_rows[:1])   or "(no morning brief today)",
        "market_data":        market_data_text,
        "market_data_label":  market_data_label,
        "breaking_top":       breaking_top_text,
        "more_stories_pool":  more_stories_pool_text,
    }


def _has_content(sources: dict[str, str]) -> bool:
    """True if at least one meaningful content source exists."""
    no_content_values = {
        "(no Trade Today today)", "(no morning brief today)",
        "(no market data available)", "(none today)",
        "(no additional stories available)",
    }
    return any(v not in no_content_values for v in sources.values())


# ── Claude newsletter generation ──────────────────────────────────────────────

async def generate_mfd_newsletter(
    api_key: str,
    model: str = "claude-opus-4-6",
) -> dict | None:
    """
    Generate a full MFD newsletter draft following the locked 9-section template.
    Returns dict: {html, subject_a, subject_b, subject_c, preview_text, plain_text}
    Returns None if content is insufficient or Claude fails.
    """
    sources = await _gather_content()

    if not _has_content(sources):
        log.info("[mfd-newsletter] no content in last 24h — skipping")
        return None

    today = datetime.date.today()
    today_str = today.strftime("%A, %B ") + str(today.day)  # e.g. "Thursday, April 10"

    user_prompt = _NEWSLETTER_USER_TMPL.format(
        date=today_str,
        trade_today=sources["trade_today"][:2000],
        morning_brief=sources["morning_brief"][:3000],
        market_data=sources["market_data"][:2000],
        market_data_label=sources["market_data_label"],
        breaking_top=sources["breaking_top"][:2000],
        more_stories_pool=sources["more_stories_pool"][:4000],
    )

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 8192,
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

        result["plain_text"] = html_to_plain(result["html"])
        return result

    except json.JSONDecodeError as exc:
        log.warning("[mfd-newsletter] JSON parse failed: %s", exc)
        return None
    except Exception as exc:
        log.warning("[mfd-newsletter] Claude call failed: %s", exc)
        return None


# ── Beehiiv API (kept for future enterprise upgrade — not called in Discord fallback mode) ──

async def _log_beehiiv_error(status_code: int, body: str, url: str) -> None:
    msg = f"[mfd-beehiiv-error] POST {url} → HTTP {status_code}\n{body[:2000]}"
    print(msg, flush=True)
    log.warning(msg)
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return
        supa_url = f"{settings.supabase_url}/rest/v1/bot_communications"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(supa_url, headers=headers, json={
                "channel": "system-logs", "message_type": "mfd-beehiiv-error",
                "content": msg, "delivery_method": "beehiiv", "status": "failed",
            })
    except Exception:
        pass


async def post_to_beehiiv(html: str, subject: str, preview_text: str,
                           api_key: str, publication_id: str) -> str | None:
    """POST a draft newsletter to Beehiiv V2 (enterprise plan required)."""
    if not publication_id.startswith("pub_"):
        publication_id = f"pub_{publication_id}"
    url = f"{BEEHIIV_API_BASE}/publications/{publication_id}/posts"
    payload = {"title": subject, "subtitle": preview_text, "status": "draft", "body_content": html}
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {api_key}",
                                            "Content-Type": "application/json"}, json=payload)
        if r.status_code in (200, 201):
            data = r.json()
            post_id = data.get("data", {}).get("id") or data.get("id", "unknown")
            log.info("[mfd-newsletter] Beehiiv draft created: %s", post_id)
            return str(post_id)
        await _log_beehiiv_error(r.status_code, r.text, url)
        return None
    except Exception as exc:
        print(f"[mfd-beehiiv-error] exception: {exc}", flush=True)
        log.warning("[mfd-beehiiv-error] %s", exc)
        return None


# ── Supabase dispatch log ─────────────────────────────────────────────────────

async def log_newsletter_dispatch(subject: str, beehiiv_id: str | None) -> None:
    """Log the newsletter draft dispatch to bot_communications. Skips if already logged today."""
    try:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return
        headers_base = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        # Dedup check: has an mfd-newsletter entry already been logged today?
        today_start = datetime.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat() + "Z"
        check_url = (
            f"{settings.supabase_url}/rest/v1/bot_communications"
            f"?channel=eq.mfd-published"
            f"&message_type=eq.mfd-newsletter"
            f"&timestamp=gte.{today_start}"
            f"&limit=1"
        )
        async with httpx.AsyncClient(timeout=10.0) as c:
            chk = await c.get(check_url, headers=headers_base)
        if chk.status_code == 200 and chk.json():
            log.info("[mfd-newsletter] dispatch already logged today — skipping duplicate")
            return

        content = f"MFD Newsletter draft — subject: {subject}"
        if beehiiv_id:
            content += f" | beehiiv_id: {beehiiv_id}"
        url = f"{settings.supabase_url}/rest/v1/bot_communications"
        headers = {**headers_base, "Content-Type": "application/json", "Prefer": "return=minimal"}
        body = {
            "channel":         "mfd-published",
            "message_type":    "mfd-newsletter",
            "content":         content,
            "delivery_method": "discord",
            "status":          "delivered",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers=headers, json=body)
        if r.status_code not in (200, 201):
            log.debug("[mfd-newsletter] supabase log %s: %s", r.status_code, r.text[:100])
    except Exception as exc:
        log.debug("[mfd-newsletter] supabase log error: %s", exc)
