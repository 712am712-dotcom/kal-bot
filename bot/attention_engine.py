"""
attention_engine.py — Causal Intelligence Engine for Kal.

Reads rss_context_today.json (populated by rss_reader.py) and morning brief data
to detect top attention signals, daily patterns, and content opportunities.

Runs 3–5 times per day between 8am–6pm ET (attention signals).
Runs once at 5pm ET (pattern report).
Uses Haiku only — target cost under $0.02/day.

Output channels:
  #attention     — top 3–5 signals per day, scored 1–10
  #patterns      — one daily pattern report at 5pm ET
  #content-queue — auto-queued when attention score >= 8 or attention+pattern overlap

Scoring logic:
  Topic in 3+ sources       → base score 8–10
  High-interest keywords    → base score 6–8
  Single source, moderate   → base score 4–6
  Only post if score >= 6
  Max 5 attention posts/day
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

# ── Scoring constants ─────────────────────────────────────────────────────────

# Topics appearing in this many distinct sources trigger score 8+
_MULTI_SOURCE_THRESHOLD = 3

# Keywords that boost score into the 6–8 range
HIGH_INTEREST_KEYWORDS: list[str] = [
    "ai", "artificial intelligence", "openai", "anthropic", "gpt",
    "federal reserve", " fed ", "rate", "inflation", " cpi ",
    "earnings", "layoffs", "recession",
    "bitcoin", "crypto", "ethereum",
    "tariff", "trade war", " china ", "war", "ceasefire",
    "s&p", "nasdaq", "market crash", "selloff",
    "oil", "energy", "jobs report", "unemployment",
]

# ── Daily state ───────────────────────────────────────────────────────────────

_attention_posts_today:   int  = 0
_attention_date:          str  = ""
_pattern_posted_today:    bool = False
_pattern_date:            str  = ""
_last_attention_check_ts: float = 0.0
_ATTENTION_MIN_INTERVAL   = 90 * 60   # at least 90 min between attention checks

MAX_ATTENTION_PER_DAY = 5


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
    today = _today_et()
    if _attention_date != today:
        _attention_posts_today = 0
        _attention_date = today
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


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_topic(topic: str, source_count: int) -> int:
    """
    Score a topic 1–10 based on source frequency and keyword relevance.
    """
    t = topic.lower()

    # Multi-source = strong signal
    if source_count >= _MULTI_SOURCE_THRESHOLD:
        return min(10, 7 + source_count - _MULTI_SOURCE_THRESHOLD)

    # Keyword boost
    for kw in HIGH_INTEREST_KEYWORDS:
        if kw in t:
            return 7

    # Single source, moderate interest
    return 4


def _extract_topics(articles: list[dict]) -> list[dict]:
    """
    Cluster articles by topic. Returns list of:
      {"topic": str, "sources": [str], "titles": [str], "score": int}
    """
    # Collect topic → sources mapping using simple word overlap
    topic_map: dict[str, dict] = {}

    for article in articles:
        title  = article.get("title", "")
        source = article.get("source", "unknown")
        words  = set(title.lower().split())

        # Try to match to existing topic bucket by keyword overlap
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
        else:
            topic_map[title[:80]] = {
                "sources": {source},
                "titles":  [title],
            }

    results = []
    for topic, data in topic_map.items():
        source_list = list(data["sources"])
        score = _score_topic(topic, len(source_list))
        if score >= 6:
            results.append({
                "topic":   topic,
                "sources": source_list,
                "titles":  data["titles"],
                "score":   score,
            })

    return sorted(results, key=lambda x: x["score"], reverse=True)


# ── Claude Haiku calls ────────────────────────────────────────────────────────

async def _haiku_attention_signal(
    topic: str, titles: list[str], score: int, api_key: str, model: str
) -> dict | None:
    """
    Call Haiku to generate a polished attention signal post.
    Returns dict with: why_matters, why_now, angle, format_suggestion
    or None on failure.
    """
    headlines = "\n".join(f"- {t}" for t in titles[:5])
    prompt = (
        f"You are Kal, a causal intelligence system that detects what people care about.\n\n"
        f"Topic: {topic}\n"
        f"Signal score: {score}/10\n"
        f"Headlines:\n{headlines}\n\n"
        f"Today: {_now_et_str()}\n\n"
        f"Respond with JSON only:\n"
        f'{{"why_matters": "<one sentence — why this topic matters right now>",\n'
        f' "why_now": "<one sentence — what specifically triggered this today>",\n'
        f' "angle": "<best content angle — how to make this relatable/simple>",\n'
        f' "format": "<slideshow | short video | thread>"}}'
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
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
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
        Posts 0–1 signals per check; respects daily max of 5.
        """
        global _attention_posts_today, _attention_date, _last_attention_check_ts

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

        articles = _load_rss_context()
        if not articles:
            log.debug("[attention] no RSS context available")
            return

        topics = _extract_topics(articles)
        if not topics:
            log.debug("[attention] no scoreable topics found")
            return

        # Pick the highest-scoring topic not yet posted today
        top = topics[0]
        score  = top["score"]
        topic  = top["topic"]
        titles = top["titles"]
        sources = top["sources"]

        if score < 6:
            log.debug("[attention] top score %d < 6 — not posting", score)
            return

        # Call Haiku to polish the signal
        result = await _haiku_attention_signal(topic, titles, score, self._api_key, self._model)

        import discord_notifier as discord
        if result:
            await discord.notify_attention_signal(
                topic=topic,
                why_matters=result.get("why_matters", ""),
                why_now=result.get("why_now", ""),
                score=score,
                sources=sources,
            )
            # Auto-queue to #content-queue if score >= 8
            if score >= 8:
                await discord.notify_content_opportunity(
                    signal=topic,
                    angle=result.get("angle", ""),
                    format_suggestion=result.get("format", "slideshow"),
                    urgency="post today" if score >= 9 else "this week",
                )
            _attention_posts_today += 1
            log.info("[attention] signal posted score=%d topic=%s", score, topic[:60])
        else:
            # Haiku failed — post basic signal without AI polish
            await discord.notify_attention_signal(
                topic=topic,
                why_matters="Multiple sources covering this topic today.",
                why_now=f"{len(sources)} sources flagged this.",
                score=score,
                sources=sources,
            )
            _attention_posts_today += 1

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
