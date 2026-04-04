"""
attention_engine.py — Causal Intelligence Engine for Kal.

Reads rss_context_today.json (populated by rss_reader.py) and morning brief data
to detect top attention signals, daily patterns, and content opportunities.

Runs 3 times per day between 8am–6pm ET (attention signals).
Runs once at 5pm ET (pattern report).
Uses Haiku only — target cost under $0.02/day.

Output channels:
  #attention     — top 3 signals per day (1 concept, 1 tool, 1 news)
  #patterns      — one daily pattern report at 5pm ET
  #content-queue — auto-queued when attention score >= 8 or attention+pattern overlap

Content filter:
  PASS if topic contains AI keywords OR source is ai_specific=True
  Only posts signals relevant to AI niche.

Signal formats:
  A — concept / educational (what something is, how it works)
  B — tool / product release (specific software, model, product)
  C — news / event (regulatory, business news, trending story)

why_now values:
  new_release  — product/model/paper just released
  trending     — gaining attention across multiple sources today
  practical    — has immediate practical application/relevance

Daily slots: max 1 per format type (A/B/C), 3 total.

Niche:
  NICHE = "AI"  — hardcoded for now, swap to "Finance" / "Crypto" etc. to fork.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_BOT_DIR = Path(__file__).parent
RSS_CONTEXT_PATH = _BOT_DIR / "rss_context_today.json"

# ── Niche config (swap this to fork for Kal_Finance, Kal_Crypto, etc.) ────────

NICHE = "AI"

# ── Scoring constants ─────────────────────────────────────────────────────────

# Topics appearing in this many distinct sources trigger score 8+
_MULTI_SOURCE_THRESHOLD = 3

# Keywords that boost score into the 6–8 range AND satisfy the AI content filter
HIGH_INTEREST_KEYWORDS: list[str] = [
    "ai", "artificial intelligence", "openai", "anthropic", "gpt",
    "federal reserve", " fed ", "rate", "inflation", " cpi ",
    "earnings", "layoffs", "recession",
    "bitcoin", "crypto", "ethereum",
    "tariff", "trade war", " china ", "war", "ceasefire",
    "s&p", "nasdaq", "market crash", "selloff",
    "oil", "energy", "jobs report", "unemployment",
]

# Content filter: both an ENTITY and an ACTION word must be present
# for a non-ai_specific source to pass. ai_specific sources need score >= 5.
_ENTITY_MATCH: set[str] = {
    "chatgpt", "claude", "gpt-4", "gpt-5", "gemini", "openai",
    "anthropic", "llama", "mistral", "copilot", "perplexity",
    "ai agent", "large language model", "llm",
}

_ACTION_WORDS: set[str] = {
    "released", "launched", "announced", "added", "update",
    "new", "banned", "acquired", "raised", "partnership",
    "replacing", "beats", "surpasses",
}

# ── Daily state ───────────────────────────────────────────────────────────────

_attention_posts_today:   int  = 0
_attention_date:          str  = ""
_pattern_posted_today:    bool = False
_pattern_date:            str  = ""
_last_attention_check_ts: float = 0.0
_ATTENTION_MIN_INTERVAL   = 90 * 60   # at least 90 min between attention checks

# Per-format daily slot tracking (max 1 per format, 3 total)
_slot_a_used: bool = False   # concept / educational
_slot_b_used: bool = False   # tool / product
_slot_c_used: bool = False   # news / event

MAX_ATTENTION_PER_DAY = 3


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


def _now_et_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def _reset_daily_state() -> None:
    global _attention_posts_today, _attention_date, _pattern_posted_today, _pattern_date
    global _slot_a_used, _slot_b_used, _slot_c_used
    today = _today_et()
    if _attention_date != today:
        _attention_posts_today = 0
        _attention_date = today
        _slot_a_used = False
        _slot_b_used = False
        _slot_c_used = False
    if _pattern_date != today:
        _pattern_posted_today = False
        _pattern_date = today


# ── RSS context loading ───────────────────────────────────────────────────────

def _load_rss_context() -> list[dict]:
    """Load today's RSS articles from rss_context_today.json. Returns [] on any error."""
    try:
        data = json.loads(RSS_CONTEXT_PATH.read_text(encoding="utf-8"))
        today = _today_et()
        if data.get("date") == today:
            return data.get("articles", [])
    except Exception:
        pass
    return []


# ── Content filter ────────────────────────────────────────────────────────────

def _passes_ai_filter(topic: str, has_ai_specific: bool, score: int) -> bool:
    """
    PASS if:
      (ai_specific == True AND score >= 5)
      OR (ENTITY_MATCH AND ACTION_WORD both present in topic text)

    Both entity AND action must be present for a non-ai_specific source.
    Eliminates ~80% of generic filler that used to slip through on loose
    keyword matching.
    """
    if has_ai_specific and score >= 5:
        return True
    t = topic.lower()
    has_entity = any(e in t for e in _ENTITY_MATCH)
    has_action = any(a in t for a in _ACTION_WORDS)
    return has_entity and has_action


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_topic(topic: str, source_count: int, has_ai_specific: bool = False) -> int:
    """
    Score a topic 1–10 based on source frequency and keyword relevance.
    ai_specific sources grant +3 to the base score.
    """
    t = topic.lower()

    # Multi-source = strong signal
    if source_count >= _MULTI_SOURCE_THRESHOLD:
        base = min(10, 7 + source_count - _MULTI_SOURCE_THRESHOLD)
    elif any(kw in t for kw in HIGH_INTEREST_KEYWORDS):
        base = 7
    else:
        base = 4

    if has_ai_specific:
        base = min(10, base + 2)

    return base


def _extract_topics(articles: list[dict]) -> list[dict]:
    """
    Cluster articles by topic. Returns list of:
      {"topic": str, "sources": [str], "titles": [str], "score": int,
       "ai_specific": bool}
    """
    topic_map: dict[str, dict] = {}

    for article in articles:
        title      = article.get("title", "")
        source     = article.get("source", "unknown")
        is_ai_spec = bool(article.get("ai_specific", False))
        words      = set(title.lower().split())

        matched = None
        for existing_topic, bucket in topic_map.items():
            existing_words = set(existing_topic.lower().split())
            overlap = len(words & existing_words - {"the", "a", "an", "in", "of", "and", "to", "is", "are", "on", "for", "with", "at", "by"})
            if overlap >= 2:
                matched = existing_topic
                break

        if matched:
            topic_map[matched]["sources"].add(source)
            topic_map[matched]["titles"].append(title)
            if is_ai_spec:
                topic_map[matched]["ai_specific"] = True
        else:
            topic_map[title[:80]] = {
                "sources":    {source},
                "titles":     [title],
                "ai_specific": is_ai_spec,
            }

    results = []
    for topic, data in topic_map.items():
        source_list    = list(data["sources"])
        has_ai_specific = data.get("ai_specific", False)
        score = _score_topic(topic, len(source_list), has_ai_specific)
        if score >= 6:
            results.append({
                "topic":      topic,
                "sources":    source_list,
                "titles":     data["titles"],
                "score":      score,
                "ai_specific": has_ai_specific,
            })

    return sorted(results, key=lambda x: x["score"], reverse=True)


# ── Claude Haiku calls ────────────────────────────────────────────────────────

async def _haiku_attention_signal(
    topic: str, titles: list[str], score: int, api_key: str, model: str
) -> dict | None:
    """
    Call Haiku to generate a structured attention signal.
    Returns the signal dict or None on failure.

    Signal shape:
      topic    — str
      format   — "A" | "B" | "C"
      hook     — str, under 12 words
      why_now  — "new_release" | "trending" | "practical"
      score    — int
      niche    — str (NICHE constant)
    """
    headlines = "\n".join(f"- {t}" for t in titles[:5])
    prompt = (
        f"You are Kal, a causal intelligence system for {NICHE} content.\n\n"
        f"Topic: {topic}\n"
        f"Signal score: {score}/10\n"
        f"Headlines:\n{headlines}\n\n"
        f"Today: {_now_et_str()}\n\n"
        f"Classify this signal and respond with JSON only:\n"
        f"{{\n"
        f'  "format": "A" | "B" | "C",\n'
        f'  "hook": "<under 12 words — the sharpest angle on this topic>",\n'
        f'  "why_now": "new_release" | "trending" | "practical",\n'
        f'  "why_matters": "<one sentence — why this matters right now>",\n'
        f'  "angle": "<best content angle — how to make this relatable/simple>"\n'
        f"}}\n\n"
        f"Format rules:\n"
        f"  A = concept / educational (explaining what something is or how it works)\n"
        f"  B = tool / product (specific software, model, or product release)\n"
        f"  C = news / event (regulatory update, business news, trending story)\n\n"
        f"why_now rules:\n"
        f"  new_release  = product/model/paper just dropped\n"
        f"  trending     = gaining attention across multiple sources today\n"
        f"  practical    = has immediate real-world use or implication"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 300,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        # Normalise / validate enum fields
        fmt = data.get("format", "C")
        if fmt not in ("A", "B", "C"):
            fmt = "C"
        why_now = data.get("why_now", "trending")
        if why_now not in ("new_release", "trending", "practical"):
            why_now = "trending"
        # Enforce hook length (truncate to 12 words)
        hook_words = data.get("hook", topic[:60]).split()
        hook = " ".join(hook_words[:12])
        return {
            "topic":       topic,
            "format":      fmt,
            "hook":        hook,
            "why_now":     why_now,
            "score":       score,
            "niche":       NICHE,
            "why_matters": data.get("why_matters", ""),
            "angle":       data.get("angle", ""),
        }
    except Exception as exc:
        log.warning("[attention] haiku_call_failed: %s", exc)
        return None


async def _haiku_pattern_report(
    articles: list[dict], api_key: str, model: str
) -> dict | None:
    """
    Call Haiku once at 5pm to identify the dominant pattern across today's sources.
    Returns dict: {pattern, evidence: [str, str, str], implication}
    """
    sample = articles[:20]
    lines  = [f"- [{a.get('source','?')}] {a.get('title','')}" for a in sample]
    prompt = (
        f"You are Kal, a causal intelligence system. Today is {_now_et_str()}.\n\n"
        f"Below are today's headlines from multiple sources:\n"
        + "\n".join(lines)
        + "\n\nIdentify the ONE dominant repeating pattern across these sources.\n"
        f"A pattern = the same underlying topic appearing across 4+ sources.\n\n"
        f"Respond with JSON only:\n"
        f'{{"pattern": "<name of the repeating pattern>",\n'
        f' "evidence": ["<source 1 example>", "<source 2 example>", "<source 3 example>"],\n'
        f' "implication": "<one sentence — what this pattern suggests is coming>"}}'
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 400,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as exc:
        log.warning("[attention] haiku_pattern_failed: %s", exc)
        return None


# ── Postability validation ────────────────────────────────────────────────────

_GENERIC_HOOK_PHRASES: set[str] = {
    "here's how",
    "learn about",
    "things you should know",
    "you need to know",
}


def validate_signal(signal: dict) -> tuple[bool, str]:
    """
    Gate: returns (True, "ok") if the signal is postable, or (False, reason).

    Rejects if:
      - Hook is under 5 words (too vague)
      - Hook contains a generic filler phrase
      - why_now is empty or "unknown"
    """
    hook = (signal.get("hook") or "").strip()
    if len(hook.split()) < 5:
        return False, "hook_too_short"
    hook_lower = hook.lower()
    for phrase in _GENERIC_HOOK_PHRASES:
        if phrase in hook_lower:
            return False, f"hook_generic_phrase({phrase!r})"
    why_now = (signal.get("why_now") or "").strip()
    if not why_now or why_now == "unknown":
        return False, "why_now_missing"
    return True, "ok"


# ── Duplicate cluster killer ──────────────────────────────────────────────────

_DEDUP_STOP_WORDS: set[str] = {
    "the", "a", "an", "in", "of", "and", "to", "is", "are",
    "on", "for", "with", "at", "by", "it", "its",
}


def _deduplicate_signals(topics: list[dict]) -> list[dict]:
    """
    Remove near-duplicate topic clusters before slot assignment.
    If two topics share 3+ significant words, keep only the higher-scoring one.
    Logs every rejection as signal_deduplicated.
    """
    kept: list[dict] = []
    for candidate in topics:
        cwords = set(candidate["topic"].lower().split()) - _DEDUP_STOP_WORDS
        duplicate = False
        for existing in kept:
            ewords = set(existing["topic"].lower().split()) - _DEDUP_STOP_WORDS
            if len(cwords & ewords) >= 3:
                log.info(
                    "[attention] signal_deduplicated kept=%r rejected=%r",
                    existing["topic"][:50],
                    candidate["topic"][:50],
                )
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


# ── Main engine ───────────────────────────────────────────────────────────────

class AttentionEngine:
    """
    Runs attention signal detection and pattern reporting.
    Instantiated once in main_async() and driven by _attention_task().
    """

    def __init__(self, api_key: str, haiku_model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self._model   = haiku_model

    async def run_attention_check(self) -> None:
        """
        Check for new attention signals. Call this every ~90 minutes during 8am–6pm ET.
        Posts 0–1 signals per check; respects daily max of 3 (1 per format slot A/B/C).

        Pipeline (enforced in order):
          1. Load articles from rss_context_today.json
          2. Score topics (ai_specific = +2 boost, cross-source frequency)
          3. Entity + action filter (_passes_ai_filter)
          4. Deduplicate near-duplicate clusters
          5. Iterate candidates: call Haiku → postability validation → slot check → post
        """
        global _attention_posts_today, _attention_date, _last_attention_check_ts
        global _slot_a_used, _slot_b_used, _slot_c_used

        _reset_daily_state()

        hour = _now_et_hour()
        if hour < 8 or hour >= 18:
            log.debug("[attention] outside attention window (8am-6pm ET) — skipping")
            return

        if _attention_posts_today >= MAX_ATTENTION_PER_DAY:
            log.debug("[attention] daily cap reached (%d posts)", MAX_ATTENTION_PER_DAY)
            return

        now = time.monotonic()
        if now - _last_attention_check_ts < _ATTENTION_MIN_INTERVAL:
            log.debug("[attention] min interval not elapsed — skipping")
            return
        _last_attention_check_ts = now

        # ── Step 1: load articles ─────────────────────────────────────────────
        articles = _load_rss_context()
        if not articles:
            log.debug("[attention] no RSS context available")
            return

        # ── Steps 2+3: score then apply entity+action content filter ──────────
        topics = _extract_topics(articles)   # scoring happens here (incl. +2 boost)
        if not topics:
            log.debug("[attention] no scoreable topics found")
            return

        filtered = [
            t for t in topics
            if t["score"] >= 6
            and _passes_ai_filter(t["topic"], t.get("ai_specific", False), t["score"])
        ]
        if not filtered:
            log.debug("[attention] no topics passed AI content filter")
            return

        # ── Step 4: deduplicate near-duplicate clusters ───────────────────────
        candidates = _deduplicate_signals(filtered)

        # ── Step 5: iterate candidates, validate, fill first open slot ────────
        import discord_notifier as discord

        for top in candidates:
            score   = top["score"]
            topic   = top["topic"]
            titles  = top["titles"]
            sources = top["sources"]

            # Call Haiku to classify and polish the signal
            signal = await _haiku_attention_signal(topic, titles, score, self._api_key, self._model)

            # Determine format; fall back to "C" only if Haiku failed entirely
            fmt = signal["format"] if signal else "C"

            # Postability validation gate
            if signal:
                ok, reason = validate_signal(signal)
                if not ok:
                    log.info(
                        "[attention] signal_rejected reason=%s topic=%s",
                        reason, topic[:60],
                    )
                    continue

            # Skip if this format slot is already filled today
            if fmt == "A" and _slot_a_used:
                log.debug("[attention] format A slot already used today — trying next")
                continue
            if fmt == "B" and _slot_b_used:
                log.debug("[attention] format B slot already used today — trying next")
                continue
            if fmt == "C" and _slot_c_used:
                log.debug("[attention] format C slot already used today — trying next")
                continue

            # ── Post the signal ───────────────────────────────────────────────
            if signal:
                await discord.notify_attention_signal(
                    topic=topic,
                    why_matters=signal.get("why_matters", ""),
                    why_now=signal.get("why_now", "trending"),
                    score=score,
                    sources=sources,
                )
                if score >= 8:
                    await discord.notify_content_opportunity(
                        signal=topic,
                        angle=signal.get("angle", ""),
                        format_suggestion=fmt,
                        urgency="post today" if score >= 9 else "this week",
                    )
            else:
                # Haiku failed — post basic signal without AI polish
                await discord.notify_attention_signal(
                    topic=topic,
                    why_matters="Multiple AI sources covering this topic today.",
                    why_now="trending",
                    score=score,
                    sources=sources,
                )

            # Mark format slot as used
            if fmt == "A":
                _slot_a_used = True
            elif fmt == "B":
                _slot_b_used = True
            else:
                _slot_c_used = True

            _attention_posts_today += 1
            log.info(
                "[attention] signal posted score=%d format=%s why_now=%s topic=%s",
                score, fmt, signal.get("why_now", "?") if signal else "?", topic[:60],
            )
            # One signal per check run
            break

    async def run_pattern_check(self) -> None:
        """
        Run the daily pattern report at 5pm ET. Posts once per day.
        """
        global _pattern_posted_today, _pattern_date

        _reset_daily_state()

        if _pattern_posted_today:
            return

        hour = _now_et_hour()
        if hour < 17:
            return

        articles = _load_rss_context()
        if not articles:
            log.debug("[attention] no articles for pattern report")
            return

        result = await _haiku_pattern_report(articles, self._api_key, self._model)

        import discord_notifier as discord
        if result:
            await discord.notify_pattern_report(
                pattern=result.get("pattern", ""),
                evidence=result.get("evidence", []),
                implication=result.get("implication", ""),
            )
            _pattern_posted_today = True
            log.info("[attention] pattern report posted: %s", result.get("pattern", "")[:60])

            # If pattern topic overlaps with any attention signal topic, queue content
            topics = _extract_topics(articles)
            if topics:
                top_topic = topics[0]["topic"].lower()
                pattern_text = result.get("pattern", "").lower()
                if any(w in pattern_text for w in top_topic.split()[:3] if len(w) > 4):
                    await discord.notify_content_opportunity(
                        signal=result.get("pattern", ""),
                        angle="Pattern-backed content — this narrative has multi-source support today.",
                        format_suggestion="thread",
                        urgency="post today",
                    )
