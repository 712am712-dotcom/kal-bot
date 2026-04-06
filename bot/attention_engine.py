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

_BOT_DIR               = Path(__file__).parent
RSS_CONTEXT_PATH       = _BOT_DIR / "rss_context_today.json"
RSS_CONTEXT_YESTERDAY  = _BOT_DIR / "rss_context_yesterday.json"
FOUNDATION_GUIDE_PATH  = _BOT_DIR / "resources" / "prompt_engineering_guide.md"

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
    # Video / audio AI tools
    "higgsfield", "seedance", "runway", "pika", "kling",
    "sora", "veo", "invideo", "synthesia", "heygen",
    "eleven labs", "elevenlabs",
}

_ACTION_WORDS: set[str] = {
    "released", "launched", "announced", "added", "update",
    "new", "banned", "acquired", "raised", "partnership",
    "replacing", "beats", "surpasses",
    # Release-specific
    "dropped", "just released", "now available", "benchmark",
}

# ── Daily state ───────────────────────────────────────────────────────────────

_attention_posts_today:   int  = 0
_attention_date:          str  = ""
_pattern_posted_today:    bool = False
_pattern_date:            str  = ""
_foundation_posted_today: bool = False
_last_attention_check_ts: float = 0.0
_ATTENTION_MIN_INTERVAL   = 90 * 60   # at least 90 min between attention checks

# Per-format daily slot tracking (max 1 per format, 4 total)
_slot_a_used: bool = False   # concept / educational
_slot_b_used: bool = False   # tool / product
_slot_c_used: bool = False   # news / event
_slot_d_used: bool = False   # community builders

MAX_ATTENTION_PER_DAY = 3   # across windows 1 + 3

# ── 3-window schedule ─────────────────────────────────────────────────────────
# Window 1: 8am-12pm  — 2-3 signals
# Window 2: 2pm-4pm   — 1 FOUNDATION post (handled by run_foundation_check)
# Window 3: 7pm-10pm  — 1-2 signals

_WINDOWS: list[tuple[int, int, int]] = [
    (8,  12, 3),   # (start_hour, end_hour, target_posts)
    (19, 22, 2),   # 7pm-10pm
]


def _current_signal_window() -> tuple[int, int] | None:
    """Return (target_posts, window_index) if we're in a signal posting window, else None."""
    hour = _now_et_hour()
    for i, (start, end, target) in enumerate(_WINDOWS):
        if start <= hour < end:
            return target, i
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


def _now_et_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def _reset_daily_state() -> None:
    global _attention_posts_today, _attention_date, _pattern_posted_today, _pattern_date
    global _slot_a_used, _slot_b_used, _slot_c_used, _slot_d_used, _foundation_posted_today
    today = _today_et()
    if _attention_date != today:
        _attention_posts_today = 0
        _attention_date = today
        _slot_a_used = False
        _slot_b_used = False
        _slot_c_used = False
        _slot_d_used = False
        _foundation_posted_today = False
    if _pattern_date != today:
        _pattern_posted_today = False
        _pattern_date = today


# ── RSS context loading ───────────────────────────────────────────────────────

def _load_rss_context(include_yesterday: bool = False) -> list[dict]:
    """
    Load today's RSS articles from rss_context_today.json.
    If include_yesterday=True, also merges yesterday's articles (48h window).
    Returns [] on any error.
    """
    articles: list[dict] = []
    try:
        data = json.loads(RSS_CONTEXT_PATH.read_text(encoding="utf-8"))
        today = _today_et()
        if data.get("date") == today:
            articles = data.get("articles", [])
    except Exception:
        pass

    if include_yesterday:
        try:
            yesterday_data = json.loads(RSS_CONTEXT_YESTERDAY.read_text(encoding="utf-8"))
            yesterday_articles = yesterday_data.get("articles", [])
            # Deduplicate by URL before merging
            existing_urls = {a.get("url", "") for a in articles}
            for a in yesterday_articles:
                if a.get("url", "") not in existing_urls:
                    articles.append(a)
            log.debug("[attention] 48h widening: merged %d yesterday articles", len(yesterday_articles))
        except Exception:
            pass

    return articles


# ── Content filter ────────────────────────────────────────────────────────────

import re as _re

# Named entities for hard entity filter (expanded from _ENTITY_MATCH)
_NAMED_ENTITIES: frozenset[str] = frozenset({
    # AI labs / models
    "openai", "anthropic", "google", "meta", "microsoft", "apple",
    "chatgpt", "claude", "gpt-4", "gpt-5", "gemini", "llama", "mistral",
    "copilot", "perplexity", "ai agent", "llm",
    # Video / audio AI
    "higgsfield", "seedance", "runway", "pika", "kling",
    "sora", "veo", "invideo", "synthesia", "heygen",
    "eleven labs", "elevenlabs",
    # Countries / geopolitical entities
    "iran", "china", "russia", "ukraine", "us", "usa", "eu", "nato",
    # Platforms / companies
    "nvidia", "intel", "amd", "tesla", "amazon", "aws",
    "samsung", "huawei", "tencent", "alibaba", "baidu",
})

_NUMBER_PATTERN = _re.compile(r'\$[\d,.]+[BMKbmk]?|\d+[\d,.]*\s*(?:billion|million|%|percent)', _re.IGNORECASE)


def _passes_entity_hard_filter(topic: str) -> bool:
    """
    Hard filter: topic must contain a named entity (company/country/product)
    OR a specific number ($30B, 40%, etc.).
    Returns True if it passes, False if it should be rejected.
    """
    t = topic.lower()
    if any(e in t for e in _NAMED_ENTITIES):
        return True
    if _NUMBER_PATTERN.search(topic):
        return True
    return False


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
        if score >= 7:
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
    Call Haiku to generate a full SIGNAL DETECTED content brief.

    Returns signal dict with: topic, title, hook, slides (list[str]),
    caption, image_prompts (dict), format, angle, why_now, score, final_score, niche.
    Returns None if Haiku rejects the signal.
    """
    headlines = "\n".join(f"- {t}" for t in titles[:5])

    prompt = f"""You are an expert AI content strategist who has grown multiple AI education pages to 1M+ followers.

ROLE: Instagram carousel content creator for @artificialeducation. Audience: beginners aged 18-35. No jargon. Punchy, emotional, direct.

TOPIC: {topic}
SIGNAL SCORE: {score}/10
HEADLINES:
{headlines}
TODAY: {_now_et_str()}

PROCESS:
1. Analyze this story — what is the specific event, entity, number, or conflict?
2. Score: novelty (1-10) + clarity (1-10) + emotional pull (1-10) + shareability (1-10)
3. Generate 3 hooks (≤12 words each), score each, pick the winner
4. Write the full carousel brief

REJECTION CRITERIA — return null immediately if ANY are true:
- No specific company, country, product, or number in the topic
- final_score below 7
- Best hook is generic ("here's how", "things to know", "you need to know") or >12 words
- Topic requires technical background knowledge to understand
- Story is not time-relevant or emotionally triggering

ACCEPTED EXAMPLES:
- "Iran threatens OpenAI $30B Stargate data center"
- "Higgsfield drops Seedance 2.0 with real-time video generation"
- "Google Gemini 2.5 beats GPT-4o on every benchmark"

REJECTED EXAMPLES:
- "The One Thing I Use AI For Every Day"
- "AI as Normal Technology"
- "Why Prompt Engineering Matters"

COMMUNITY_BUILDERS CHECK — before choosing format, evaluate:
Does this topic involve a major AI model/tool released within the last 5 days AND do you know of
real community builds (things people have actually made with it, with engagement signals like
likes/retweets)? If YES → use format D. If NO → use A/B/C as normal.

FORMAT D REJECTION CRITERIA (in addition to global rejections above):
- Cannot name at least 3 specific, distinct things people actually built with this tool
- Engagement signals are weak or unknown (< 1000 likes equivalent)
- Examples are vague ("someone used it for work"), repetitive, or unimpressive
- Release is older than 5 days

OUTPUT (return null if rejected, otherwise this exact JSON):
{{
  "title": "<specific topic with real entity/event — not generic>",
  "hook": "<winning hook, ≤12 words, must contain company name OR country OR number OR specific event>",
  "hook_reason": "<one sentence — why this hook wins over the other two>",
  "slides": [
    "Slide 1: <real media instruction — grab screenshot of X, use clip from Y, show the actual announcement>",
    "Slide 2: <community proof example 1 — what was built, specific output, engagement metric>",
    "Slide 3: <community proof example 2 — what was built, specific output, engagement metric>",
    "Slide 4: <community proof example 3 — what was built, specific output, engagement metric>",
    "Slide 5: <implication — what this speed/capability shift means, what's now possible>",
    "Slide 6: Follow @artificialeducation — <punchy CTA related to topic>"
  ],
  "community_proof": [
    {{"build": "<specific thing built — tool used, what was created, who built it>", "metric": "<engagement: e.g. 4.2K likes, 800 retweets>"}},
    {{"build": "<specific thing built>", "metric": "<engagement>"}},
    {{"build": "<specific thing built>", "metric": "<engagement>"}}
  ],
  "caption": "<full ready-to-paste caption — mobile format, short lines, no educational tone, punchy, 150-300 words, ends with question or CTA>",
  "image_prompts": {{
    "slide_1": "real media — <specific instruction: screenshot of [X], clip from [Y], or photo of [event]>",
    "slides_2_6": "Kodachrome film still, <subject related to slide content>, <mood>, cinematic hyperrealistic, motion blur, anti aliasing, lens distortion, color accent lighting, 2020s, no text, no watermark, vertical format, gorgeous, highly detailed"
  }},
  "format": "A" | "B" | "C" | "D",
  "angle": "perspective_shift" | "economic_impact" | "conflict",
  "why_now": "new_release" | "trending" | "practical",
  "final_score": <integer 7-10>
}}

FORMAT RULES:
  A = concept/educational (explaining what something is or how it works)
  B = tool/product release (specific software, model, product drop)
  C = news/event (regulatory, business news, geopolitical, trending story)
  D = community builders (major recent release + 3 real builds with strong engagement)
    — slides 2-4 MUST be the community proof examples
    — slides 5-6 are implication + CTA
    — community_proof field is REQUIRED for format D; omit it for A/B/C

ANGLE RULES:
  perspective_shift — challenges a common belief ("AI isn't going to take your job. AI users are.")
  economic_impact   — involves cost, jobs, money, or industry disruption ("What studios spend $200M on now costs $0")
  conflict          — displacement, competition, or battle between forces ("The real war isn't human vs AI")"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 1800,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        if text.lower() == "null":
            log.info("[attention] haiku_rejected topic=%s", topic[:60])
            return None

        data = json.loads(text)
        if data is None:
            return None

        # Validate / normalise enum fields
        fmt = data.get("format", "C")
        if fmt not in ("A", "B", "C", "D"):
            fmt = "C"
        why_now = data.get("why_now", "trending")
        if why_now not in ("new_release", "trending", "practical"):
            why_now = "trending"
        angle = data.get("angle", "perspective_shift")
        if angle not in ("perspective_shift", "economic_impact", "conflict"):
            angle = "perspective_shift"
        final_score = int(data.get("final_score", score))
        if final_score < 7:
            log.info("[attention] haiku_score_too_low final_score=%d topic=%s", final_score, topic[:60])
            return None

        # Format D: validate community_proof has 3 entries with real builds + metrics
        community_proof: list[dict] = []
        if fmt == "D":
            raw_proof = data.get("community_proof") or []
            if not isinstance(raw_proof, list):
                raw_proof = []
            for entry in raw_proof[:3]:
                build  = (entry.get("build") or "").strip()
                metric = (entry.get("metric") or "").strip()
                if build and metric:
                    community_proof.append({"build": build, "metric": metric})
            if len(community_proof) < 3:
                log.info("[attention] community_builders_rejected insufficient_proof=%d topic=%s",
                         len(community_proof), topic[:60])
                return None

        # Enforce hook length (truncate to 12 words)
        hook_words = (data.get("hook") or topic[:60]).split()
        hook = " ".join(hook_words[:12])

        # Ensure slides is a list with exactly 6 entries
        slides = data.get("slides") or []
        if not isinstance(slides, list):
            slides = []
        while len(slides) < 6:
            slides.append("")

        return {
            "topic":           topic,
            "title":           data.get("title", topic[:80]),
            "format":          fmt,
            "hook":            hook,
            "hook_reason":     data.get("hook_reason", ""),
            "slides":          slides[:6],
            "community_proof": community_proof,   # [] for A/B/C, 3 entries for D
            "caption":         data.get("caption", ""),
            "image_prompts":   data.get("image_prompts", {}),
            "why_now":         why_now,
            "angle":           angle,
            "score":           score,
            "final_score":     final_score,
            "niche":           NICHE,
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


async def _haiku_foundation_post(api_key: str, model: str) -> dict | None:
    """
    Generate a FOUNDATION post from prompt_engineering_guide.md.
    Evergreen, opinionated, not news-dependent. Always format A.
    Posts once per day at 2pm ET.

    Returns dict: {title, hook, slides, caption, image_prompts, format, angle, why_now}
    """
    try:
        guide_text = FOUNDATION_GUIDE_PATH.read_text(encoding="utf-8")[:3000]
    except Exception as exc:
        log.warning("[attention] foundation_guide_read_failed: %s", exc)
        return None

    prompt = f"""You are an expert AI content strategist for @artificialeducation on Instagram. Audience: beginners aged 18-35.

TASK: Create one evergreen, high-performing Instagram carousel about prompt engineering or AI usage.

SOURCE MATERIAL:
{guide_text}

RULES:
- Pick ONE specific, opinionated angle — not "what is prompt engineering"
- Must be immediately actionable or challenge a common belief
- No news dependency — this must be relevant any day of the year

GOOD TOPICS:
- "Stop prompting like this (most people do it wrong)"
- "3 prompts that replace 4 hours of work"
- "The prompt structure that works 90% of the time"
- "Why your AI answers are bad (it's not the AI)"

BAD TOPICS (reject these angles):
- "What is prompt engineering?"
- "Benefits of using AI"
- "Introduction to ChatGPT"

OUTPUT (this exact JSON, no preamble):
{{
  "title": "<specific, opinionated title>",
  "hook": "<hook ≤12 words — punchy, specific, must create curiosity or mild tension>",
  "slides": [
    "Slide 1: <title card text — short, punchy, the hook>",
    "Slide 2: <the problem most people have (relatable)>",
    "Slide 3: <the fix — specific, actionable>",
    "Slide 4: <example — before/after or real prompt shown>",
    "Slide 5: <why this works — the principle behind it>",
    "Slide 6: Follow @artificialeducation — <CTA: save this, share with a friend who uses AI>"
  ],
  "caption": "<full ready-to-paste caption — mobile format, short lines, punchy, no fluff, 100-200 words>",
  "image_prompts": {{
    "slide_1": "Kodachrome film still, person confidently typing at laptop late at night, warm glow, cinematic hyperrealistic, motion blur, anti aliasing, lens distortion, color accent lighting, 2020s, no text, no watermark, vertical format, gorgeous, highly detailed",
    "slides_2_6": "Kodachrome film still, <subject matching each slide's theme>, thoughtful mood, cinematic hyperrealistic, motion blur, anti aliasing, lens distortion, color accent lighting, 2020s, no text, no watermark, vertical format, gorgeous, highly detailed"
  }},
  "format": "A",
  "angle": "perspective_shift",
  "why_now": "practical"
}}"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      model,
                    "max_tokens": 1600,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        data = json.loads(text)
        if not data:
            return None
        slides = data.get("slides") or []
        if not isinstance(slides, list):
            slides = []
        while len(slides) < 6:
            slides.append("")
        return {
            "title":         data.get("title", ""),
            "hook":          " ".join((data.get("hook") or "").split()[:12]),
            "slides":        slides[:6],
            "caption":       data.get("caption", ""),
            "image_prompts": data.get("image_prompts", {}),
            "format":        "A",
            "angle":         "perspective_shift",
            "why_now":       "practical",
            "final_score":   8,
            "niche":         NICHE,
        }
    except Exception as exc:
        log.warning("[attention] haiku_foundation_failed: %s", exc)
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
        Check for new attention signals. Runs every 30 min; internal throttle = 90 min.

        Schedule:
          Window 1 — 8am-12pm ET  (target 2-3 signals)
          Window 3 — 7pm-10pm ET  (target 1-2 signals)
          Foundation at 2pm is handled separately by run_foundation_check().

        Pipeline (enforced in order):
          1. Check we're in a signal window, daily cap not reached, interval elapsed
          2. Load articles (48h widening if today < 3 posts)
          3. Score → entity+action filter → entity hard filter → dedup
          4. Iterate candidates: Haiku → postability validation → slot check → post
        """
        global _attention_posts_today, _attention_date, _last_attention_check_ts
        global _slot_a_used, _slot_b_used, _slot_c_used, _slot_d_used

        _reset_daily_state()

        # ── Window check ──────────────────────────────────────────────────────
        window = _current_signal_window()
        if window is None:
            log.debug("[attention] outside signal windows — skipping")
            return

        if _attention_posts_today >= MAX_ATTENTION_PER_DAY:
            log.debug("[attention] daily cap reached (%d posts)", MAX_ATTENTION_PER_DAY)
            return

        now = time.monotonic()
        if now - _last_attention_check_ts < _ATTENTION_MIN_INTERVAL:
            log.debug("[attention] min interval not elapsed — skipping")
            return
        _last_attention_check_ts = now

        # ── Step 1: load articles (widen to 48h if we're short on signals) ────
        include_yesterday = (_attention_posts_today < 3)
        articles = _load_rss_context(include_yesterday=include_yesterday)
        if not articles:
            log.debug("[attention] no RSS context available")
            return

        # ── Steps 2+3: score → AI filter → entity hard filter ─────────────────
        topics = _extract_topics(articles)
        if not topics:
            log.debug("[attention] no scoreable topics found")
            return

        filtered = [
            t for t in topics
            if t["score"] >= 7
            and _passes_ai_filter(t["topic"], t.get("ai_specific", False), t["score"])
            and _passes_entity_hard_filter(t["topic"])
        ]
        if not filtered:
            log.debug("[attention] no topics passed content filters (score≥7 + entity)")
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

            # Call Haiku to generate the full SIGNAL DETECTED brief
            signal = await _haiku_attention_signal(topic, titles, score, self._api_key, self._model)

            if signal is None:
                # Haiku rejected — try next candidate
                continue

            fmt = signal["format"]

            # Postability validation gate
            ok, reason = validate_signal(signal)
            if not ok:
                log.info("[attention] signal_rejected reason=%s topic=%s", reason, topic[:60])
                continue

            # Skip if this format slot is already filled today
            if fmt == "A" and _slot_a_used:
                log.debug("[attention] format A slot full — trying next")
                continue
            if fmt == "B" and _slot_b_used:
                log.debug("[attention] format B slot full — trying next")
                continue
            if fmt == "C" and _slot_c_used:
                log.debug("[attention] format C slot full — trying next")
                continue
            if fmt == "D" and _slot_d_used:
                log.debug("[attention] format D slot full — trying next")
                continue

            # ── Post to #attention and #content-queue ─────────────────────────
            await discord.notify_attention_signal(
                topic=topic,
                why_matters=signal.get("hook", ""),
                why_now=signal.get("why_now", "trending"),
                score=score,
                sources=sources,
            )
            await discord.notify_content_opportunity(signal)

            # Mark format slot as used
            if fmt == "A":
                _slot_a_used = True
            elif fmt == "B":
                _slot_b_used = True
            elif fmt == "D":
                _slot_d_used = True
            else:
                _slot_c_used = True

            _attention_posts_today += 1
            log.info(
                "[attention] signal_posted score=%d format=%s why_now=%s final_score=%d topic=%s",
                score, fmt, signal["why_now"], signal["final_score"], topic[:60],
            )
            # One signal per check run
            break

    async def run_foundation_check(self) -> None:
        """
        Post one FOUNDATION post to #content-queue at 2pm ET. Once per day.
        Evergreen prompt-engineering content from prompt_engineering_guide.md.
        """
        global _foundation_posted_today

        _reset_daily_state()

        if _foundation_posted_today:
            return

        hour = _now_et_hour()
        if hour < 14 or hour >= 16:
            return

        import discord_notifier as discord

        signal = await _haiku_foundation_post(self._api_key, self._model)
        if signal:
            # Add FOUNDATION marker so notifier formats it distinctly
            signal["is_foundation"] = True
            await discord.notify_content_opportunity(signal)
            _foundation_posted_today = True
            log.info("[attention] foundation_post sent hook=%s", signal.get("hook", "")[:60])
        else:
            log.warning("[attention] foundation_post generation failed")

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
                    await discord.notify_content_opportunity({
                        "title":   result.get("pattern", ""),
                        "hook":    result.get("pattern", "")[:80],
                        "slides":  [],
                        "caption": result.get("implication", ""),
                        "image_prompts": {},
                        "format":  "C",
                        "angle":   "perspective_shift",
                        "why_now": "trending",
                        "final_score": 8,
                    })
