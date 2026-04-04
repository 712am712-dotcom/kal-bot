"""
owner_commands.py — Structured command router for "Kal: <command>" messages.

Handles specific commands from #system-logs. Called by _owner_query_task in main.py.
Falls back to a generic Haiku response for unrecognised queries.

Commands (case-insensitive, leading/trailing whitespace stripped):
  did you post today?    — check content_generations + signals tables
  show unused signals    — top 5 unposted signals ranked by score
  generate next post     — pull top signal, call Storyforge, return preview
  approve                — mark pending signal as posted in Supabase
  skip                   — discard pending signal, ready for next
  status                 — daily stats: signals, posts, RSS articles, cost
"""
from __future__ import annotations

import datetime
import logging

import httpx

log = logging.getLogger(__name__)

# Module-level state: signal awaiting approve / skip
_pending_signal: dict | None = None


# ── Supabase helper ───────────────────────────────────────────────────────────

async def _sb_get(
    sb_url: str,
    sb_key: str,
    table: str,
    params: dict,
) -> list[dict]:
    """
    GET rows from a Supabase table via the REST API.
    Returns [] on any error (tables may not exist yet).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{sb_url}/rest/v1/{table}",
                headers={
                    "apikey":        sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Accept":        "application/json",
                },
                params=params,
            )
        if r.status_code in (200, 206):
            return r.json() if isinstance(r.json(), list) else []
        log.debug("[owner_cmd] sb_get %s HTTP %d", table, r.status_code)
        return []
    except Exception as exc:
        log.debug("[owner_cmd] sb_get %s failed: %s", table, exc)
        return []


async def _sb_patch(
    sb_url: str,
    sb_key: str,
    table: str,
    filters: dict,
    updates: dict,
) -> bool:
    """PATCH (update) rows. Returns True on success."""
    try:
        # Build filter query string (e.g. {"id": "eq.abc"} → ?id=eq.abc)
        params = {k: v for k, v in filters.items()}
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.patch(
                f"{sb_url}/rest/v1/{table}",
                headers={
                    "apikey":        sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Content-Type":  "application/json",
                    "Prefer":        "return=minimal",
                },
                params=params,
                json=updates,
            )
        return r.status_code in (200, 204)
    except Exception as exc:
        log.debug("[owner_cmd] sb_patch %s failed: %s", table, exc)
        return False


async def _sb_post(
    sb_url: str,
    sb_key: str,
    table: str,
    row: dict,
) -> str:
    """INSERT a row. Returns the new row id or empty string."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{sb_url}/rest/v1/{table}",
                headers={
                    "apikey":        sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Content-Type":  "application/json",
                    "Prefer":        "return=representation",
                },
                json=row,
            )
        if r.status_code in (200, 201):
            data = r.json()
            return data[0].get("id", "") if data else ""
        return ""
    except Exception as exc:
        log.debug("[owner_cmd] sb_post %s failed: %s", table, exc)
        return ""


# ── Storyforge helper ─────────────────────────────────────────────────────────

async def _storyforge_generate(
    signal: dict,
    storyforge_url: str,
    storyforge_key: str,
) -> dict | None:
    """
    Call Storyforge /api/v2/generate with the signal data.
    Returns the parsed response dict or None on failure.

    Payload shape sent to Storyforge:
      topic        — signal topic string
      hook         — under-12-word hook
      format       — A | B | C
      angle        — perspective_shift | economic_impact | conflict
      why_now      — new_release | trending | practical
      niche        — AI
      slide_count  — 6
    """
    if not storyforge_url or not storyforge_key:
        return None
    try:
        payload = {
            "topic":       signal.get("topic", ""),
            "hook":        signal.get("hook", ""),
            "format":      signal.get("format", "C"),
            "angle":       signal.get("angle", "perspective_shift"),
            "why_now":     signal.get("why_now", "trending"),
            "niche":       signal.get("niche", "AI"),
            "slide_count": 6,
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{storyforge_url.rstrip('/')}/api/v2/generate",
                headers={
                    "Authorization": f"Bearer {storyforge_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        if r.status_code == 200:
            return r.json()
        log.warning("[owner_cmd] storyforge HTTP %d: %s", r.status_code, r.text[:200])
        return None
    except Exception as exc:
        log.warning("[owner_cmd] storyforge_generate failed: %s", exc)
        return None


# ── Command handlers ──────────────────────────────────────────────────────────

async def _cmd_did_you_post_today(sb_url: str, sb_key: str) -> str:
    """Check content_generations for today. Show top 3 unused signals if nothing posted."""
    today = datetime.date.today().isoformat()

    rows = await _sb_get(sb_url, sb_key, "content_generations", {
        "select": "topic,score,format,created_at",
        "created_at": f"gte.{today}",
        "order": "score.desc",
        "limit": "5",
    })

    if rows:
        lines = ["**Posted today:**"]
        for r in rows:
            lines.append(
                f"• [{r.get('format', '?')}] {r.get('topic', '?')[:60]} — score {r.get('score', '?')}"
            )
        return "\n".join(lines)

    # Nothing posted — show top 3 unused signals
    unused = await _sb_get(sb_url, sb_key, "signals", {
        "select": "topic,score,format,angle,why_now",
        "posted": "eq.false",
        "order": "score.desc",
        "limit": "3",
    })

    if not unused:
        return "Nothing posted today. No unused signals found — RSS pipeline may still be warming up."

    lines = ["Nothing posted today yet. **Top unused signals:**"]
    for i, s in enumerate(unused, 1):
        lines.append(
            f"{i}. [{s.get('format', '?')}] {s.get('topic', '?')[:70]}\n"
            f"   Score {s.get('score', '?')} · {s.get('why_now', '?')} · {s.get('angle', '?')}"
        )
    return "\n".join(lines)


async def _cmd_show_unused_signals(sb_url: str, sb_key: str) -> str:
    """Return top 5 unposted signals ranked by score."""
    rows = await _sb_get(sb_url, sb_key, "signals", {
        "select": "id,topic,score,format,angle,why_now,hook",
        "posted": "eq.false",
        "order": "score.desc",
        "limit": "5",
    })

    if not rows:
        return "No unused signals found. Kal will generate more during the next attention check."

    lines = ["**Top unused signals (unposted):**"]
    for i, s in enumerate(rows, 1):
        lines.append(
            f"{i}. [{s.get('format', '?')}] **{s.get('topic', '?')[:60]}**\n"
            f"   Hook: _{s.get('hook', '—')[:80]}_\n"
            f"   Score {s.get('score', '?')} · {s.get('why_now', '?')} · {s.get('angle', '?')}"
        )
    return "\n".join(lines)


async def _cmd_generate_next_post(
    sb_url: str,
    sb_key: str,
    storyforge_url: str,
    storyforge_key: str,
) -> str:
    """Pull top unused signal, call Storyforge, return preview. Saves pending state."""
    global _pending_signal

    rows = await _sb_get(sb_url, sb_key, "signals", {
        "select": "id,topic,score,format,angle,why_now,hook,niche",
        "posted": "eq.false",
        "order": "score.desc",
        "limit": "1",
    })

    if not rows:
        return "No unused signals available. Check back after the next attention scan."

    signal = rows[0]
    _pending_signal = signal

    if not storyforge_url:
        return (
            f"**Signal ready (Storyforge not configured):**\n"
            f"Topic: {signal.get('topic', '?')}\n"
            f"Hook: _{signal.get('hook', '—')}_\n"
            f"Format: {signal.get('format', '?')} · Score: {signal.get('score', '?')}\n"
            f"Angle: {signal.get('angle', '?')} · Why now: {signal.get('why_now', '?')}\n\n"
            f"Reply **Kal: approve** to mark as used, or **Kal: skip** to move to next."
        )

    result = await _storyforge_generate(signal, storyforge_url, storyforge_key)

    if not result:
        return (
            f"**Signal ready (Storyforge call failed — check STORYFORGE_URL / STORYFORGE_API_KEY):**\n"
            f"Topic: {signal.get('topic', '?')}\n"
            f"Hook: _{signal.get('hook', '—')}_\n\n"
            f"Reply **Kal: approve** to mark as used, or **Kal: skip** to move to next."
        )

    # Parse Storyforge response — adapt to actual API shape
    slides = result.get("slides", result.get("content", []))
    caption = result.get("caption", result.get("description", ""))

    lines = [
        f"**Preview for:** {signal.get('topic', '?')[:60]}",
        f"Hook: _{signal.get('hook', '—')}_",
        f"Format {signal.get('format', '?')} · Score {signal.get('score', '?')} · {signal.get('angle', '?')}",
        "",
    ]

    if slides:
        lines.append("**Slides:**")
        for i, slide in enumerate(slides[:6], 1):
            text = slide.get("text", slide) if isinstance(slide, dict) else str(slide)
            lines.append(f"  {i}. {str(text)[:120]}")

    if caption:
        lines.append(f"\n**Caption:** {str(caption)[:300]}")

    lines.append("\nReply **Kal: approve** to mark as used, or **Kal: skip** to move to next.")
    return "\n".join(lines)


async def _cmd_approve(sb_url: str, sb_key: str) -> str:
    """Mark pending signal as posted in Supabase + log to content_generations."""
    global _pending_signal

    if not _pending_signal:
        return "No pending signal to approve. Run **Kal: generate next post** first."

    signal = _pending_signal
    sig_id = signal.get("id", "")

    if sig_id and sb_url:
        ok = await _sb_patch(sb_url, sb_key, "signals", {"id": f"eq.{sig_id}"}, {"posted": True})
        if ok:
            await _sb_post(sb_url, sb_key, "content_generations", {
                "signal_id": sig_id,
                "topic":     signal.get("topic", ""),
                "score":     signal.get("score", 0),
                "format":    signal.get("format", ""),
                "angle":     signal.get("angle", ""),
                "why_now":   signal.get("why_now", ""),
                "hook":      signal.get("hook", ""),
            })
            _pending_signal = None
            return (
                f"Signal marked as posted.\n"
                f"Topic: **{signal.get('topic', '?')[:60]}**\n"
                f"Content is ready — download from Storyforge and post."
            )
        else:
            _pending_signal = None
            return "Supabase update failed — signal may not exist yet. Cleared pending state."

    _pending_signal = None
    return "Approved (no Supabase URL configured — signal cleared from memory only)."


async def _cmd_skip(sb_url: str, sb_key: str) -> str:
    """Skip the pending signal and clear it so the next one can be generated."""
    global _pending_signal

    if not _pending_signal:
        return "No pending signal to skip."

    topic = _pending_signal.get("topic", "?")[:60]
    _pending_signal = None
    return f"Skipped: _{topic}_\nRun **Kal: generate next post** to pull the next signal."


async def _cmd_status(sb_url: str, sb_key: str) -> str:
    """Show daily system stats: signals, posts, RSS articles, cost."""
    today = datetime.date.today().isoformat()
    lines = [f"**Kal Status — {today}**"]

    # Signals found today
    signals_today = await _sb_get(sb_url, sb_key, "signals", {
        "select": "id",
        "created_at": f"gte.{today}",
    })
    lines.append(f"Signals found today: **{len(signals_today)}**")

    # Posts generated today
    posts_today = await _sb_get(sb_url, sb_key, "content_generations", {
        "select": "id",
        "created_at": f"gte.{today}",
    })
    lines.append(f"Posts generated today: **{len(posts_today)}**")

    # RSS articles processed today
    rss_today = await _sb_get(sb_url, sb_key, "rss_articles", {
        "select": "id",
        "created_at": f"gte.{today}",
    })
    lines.append(f"RSS articles processed: **{len(rss_today)}**")

    # Daily API cost from ai_decisions
    decisions_today = await _sb_get(sb_url, sb_key, "ai_decisions", {
        "select": "id",
        "created_at": f"gte.{today}",
    })
    lines.append(f"AI decisions today: **{len(decisions_today)}**")

    # Pending signal
    if _pending_signal:
        lines.append(f"Pending approval: _{_pending_signal.get('topic', '?')[:50]}_")

    lines.append("System health: **online**")
    return "\n".join(lines)


# ── Command router ────────────────────────────────────────────────────────────

async def dispatch(
    query: str,
    sb_url: str,
    sb_key: str,
    storyforge_url: str = "",
    storyforge_key: str = "",
) -> str | None:
    """
    Route a "Kal: <query>" string to the right handler.
    Returns a response string, or None if the query is unrecognised
    (caller should fall through to generic Haiku response).
    """
    q = query.lower().strip()

    if q in ("did you post today?", "did you post today"):
        return await _cmd_did_you_post_today(sb_url, sb_key)

    if q in ("show unused signals", "show signals", "unused signals"):
        return await _cmd_show_unused_signals(sb_url, sb_key)

    if q in ("generate next post", "generate post", "next post"):
        return await _cmd_generate_next_post(sb_url, sb_key, storyforge_url, storyforge_key)

    if q == "approve":
        return await _cmd_approve(sb_url, sb_key)

    if q == "skip":
        return await _cmd_skip(sb_url, sb_key)

    if q == "status":
        return await _cmd_status(sb_url, sb_key)

    return None  # unknown command — caller uses generic Haiku fallback
