# Module 2 Extract — ae-intel Rebuild Handoff

This file preserves all logic, prompts, schemas, and configuration from Kal's
Module 2 (Content/Attention Intelligence) before it was removed from the main
Kal trading bot. Use this as the source of truth when rebuilding Module 2 as a
standalone agent (`ae-intel`).

Removed from Kal on: 2026-04-19
Kal commit just prior to removal: see `git log` before that date.

---

## 1. Architecture Overview

Module 2 consisted of three components layered on top of Module 1's RSS feeds:

```
RSS feeds (rss_reader.py, Module 1)
  └─ rss_context_today.json  ← rolling context file (max 40 articles/day)
       └─ attention_engine.py  ← scores topics, calls Haiku, posts to #attention / #content-queue
            └─ content_jobs table  ← ae-signal jobs picked up by Content Engine
       └─ ideas_channel.py  ← spots non-mandate opportunities, posts to #ideas
```

---

## 2. File: `attention_engine.py`

### Niche config
```python
NICHE = "AI"  # swap to "Finance" / "Crypto" to fork for different niche pages
```

### Scoring constants
```python
_MULTI_SOURCE_THRESHOLD = 3   # 3+ sources on same topic → score 8+

HIGH_INTEREST_KEYWORDS = [
    "ai", "artificial intelligence", "openai", "anthropic", "gpt",
    "federal reserve", " fed ", "rate", "inflation", " cpi ",
    "earnings", "layoffs", "recession",
    "bitcoin", "crypto", "ethereum",
    "tariff", "trade war", " china ", "war", "ceasefire",
    "s&p", "nasdaq", "market crash", "selloff",
    "oil", "energy", "jobs report", "unemployment",
]

# Hard entity filter — topic must contain entity OR a number ($30B, 40%) to pass
_NAMED_ENTITIES = frozenset({
    "openai", "anthropic", "google", "meta", "microsoft", "apple",
    "chatgpt", "claude", "gpt-4", "gpt-5", "gemini", "llama", "mistral",
    "copilot", "perplexity", "ai agent", "llm",
    "higgsfield", "seedance", "runway", "pika", "kling",
    "sora", "veo", "invideo", "synthesia", "heygen",
    "eleven labs", "elevenlabs",
    "iran", "china", "russia", "ukraine", "us", "usa", "eu", "nato",
    "nvidia", "intel", "amd", "tesla", "amazon", "aws",
    "samsung", "huawei", "tencent", "alibaba", "baidu",
})
_NUMBER_PATTERN = re.compile(r'\$[\d,.]+[BMKbmk]?|\d+[\d,.]*\s*(?:billion|million|%|percent)', re.IGNORECASE)

# AI filter — entity + action both required for non-ai_specific sources
_ENTITY_MATCH = {
    "chatgpt", "claude", "gpt-4", "gpt-5", "gemini", "openai",
    "anthropic", "llama", "mistral", "copilot", "perplexity",
    "ai agent", "large language model", "llm",
    "higgsfield", "seedance", "runway", "pika", "kling",
    "sora", "veo", "invideo", "synthesia", "heygen",
    "eleven labs", "elevenlabs",
}

_ACTION_WORDS = {
    "released", "launched", "announced", "added", "update",
    "new", "banned", "acquired", "raised", "partnership",
    "replacing", "beats", "surpasses",
    "dropped", "just released", "now available", "benchmark",
}
```

### Scoring function
```python
def _score_topic(topic, source_count, has_ai_specific=False):
    if source_count >= 3:
        base = min(10, 7 + source_count - 3)
    elif any(kw in topic.lower() for kw in HIGH_INTEREST_KEYWORDS):
        base = 7
    else:
        base = 4
    if has_ai_specific:
        base = min(10, base + 2)
    return base
```

### Filter functions
```python
def _passes_entity_hard_filter(topic):
    # Returns True if named entity OR number present
    ...

def _passes_ai_filter(topic, has_ai_specific, score):
    # PASS if (ai_specific AND score>=5) OR (entity+action both present)
    ...
```

### Daily state
- Max 3 attention signals per day (`MAX_ATTENTION_PER_DAY = 3`)
- One slot per format type: A (concept), B (tool), C (news), D (community builders)
- Min 90 min between attention checks (`_ATTENTION_MIN_INTERVAL = 90 * 60`)
- Schedule windows:
  - Window 1: 8am–12pm ET (target 3 signals)
  - Window 2: 7pm–10pm ET (target 2 signals)
  - Foundation post: 2pm ET (from `prompt_engineering_guide.md`)
  - Pattern report: 5pm ET

### Signal deduplication
- Two topics sharing 3+ significant words → keep higher-scoring one

### Postability validation gate
```python
REJECT if:
  - hook under 5 words
  - hook contains: "here's how", "learn about", "things you should know", "you need to know"
  - why_now is empty or "unknown"
```

---

## 3. Haiku Prompt: Attention Signal (`_haiku_attention_signal`)

Full prompt sent to Claude Haiku for each candidate topic:

```
You are an expert AI content strategist who has grown multiple AI education pages to 1M+ followers.

ROLE: Instagram carousel content creator for @artificialeducation. Audience: beginners aged 18-35. No jargon. Punchy, emotional, direct.

TOPIC: {topic}
SIGNAL SCORE: {score}/10
HEADLINES:
{headlines}
TODAY: {now_et_str}

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
{
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
    {"build": "<specific thing built — tool used, what was created, who built it>", "metric": "<engagement: e.g. 4.2K likes, 800 retweets>"},
    {"build": "<specific thing built>", "metric": "<engagement>"},
    {"build": "<specific thing built>", "metric": "<engagement>"}
  ],
  "caption": "<full ready-to-paste caption — mobile format, short lines, no educational tone, punchy, 150-300 words, ends with question or CTA>",
  "image_prompts": {
    "slide_1": "real media — <specific instruction: screenshot of [X], clip from [Y], or photo of [event]>",
    "slides_2_6": "Kodachrome film still, <subject related to slide content>, <mood>, cinematic hyperrealistic, motion blur, anti aliasing, lens distortion, color accent lighting, 2020s, no text, no watermark, vertical format, gorgeous, highly detailed"
  },
  "format": "A" | "B" | "C" | "D",
  "angle": "perspective_shift" | "economic_impact" | "conflict",
  "why_now": "new_release" | "trending" | "practical",
  "final_score": <integer 7-10>
}

FORMAT RULES:
  A = concept/educational (explaining what something is or how it works)
  B = tool/product release (specific software, model, product drop)
  C = news/event (regulatory, business news, geopolitical, trending story)
  D = community builders (major recent release + 3 real builds with strong engagement)
    — slides 2-4 MUST be the community proof examples
    — slides 5-6 are implication + CTA
    — community_proof field is REQUIRED for format D; omit it for A/B/C

ANGLE RULES:
  perspective_shift — challenges a common belief
  economic_impact   — involves cost, jobs, money, or industry disruption
  conflict          — displacement, competition, or battle between forces
```

---

## 4. Haiku Prompt: Pattern Report (`_haiku_pattern_report`)

```
You are Kal, a causal intelligence system. Today is {now_et_str}.

Below are today's headlines from multiple sources:
{headlines}  # up to 20, format: "- [source] title"

Identify the ONE dominant repeating pattern across these sources.
A pattern = the same underlying topic appearing across 4+ sources.

Respond with JSON only:
{
  "pattern": "<name of the repeating pattern>",
  "evidence": ["<source 1 example>", "<source 2 example>", "<source 3 example>"],
  "implication": "<one sentence — what this pattern suggests is coming>"
}
```

---

## 5. Haiku Prompt: Foundation Post (`_haiku_foundation_post`)

Uses `bot/resources/prompt_engineering_guide.md` (first 3000 chars) as source material.

```
You are an expert AI content strategist for @artificialeducation on Instagram. Audience: beginners aged 18-35.

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
{
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
  "image_prompts": {
    "slide_1": "Kodachrome film still, ...",
    "slides_2_6": "Kodachrome film still, <subject matching each slide's theme>, ..."
  },
  "format": "A",
  "angle": "perspective_shift",
  "why_now": "practical"
}
```

---

## 6. File: `ideas_channel.py`

### System prompt
```python
IDEA_SYSTEM_PROMPT = KAL_IDENTITY + """
Your current task: Flag an opportunity OUTSIDE your current trading mandate.
Current mandate: Kalshi prediction markets (crypto) and crypto spot trades (BTC/ETH/SOL).
You are posting to the private #ideas channel for your CEO to evaluate.

You execute Kalshi and crypto autonomously. You post here ONLY for:
- Stocks and sector ETFs
- Bonds and rates trades
- Commodities (oil, gold, silver)
- New Kalshi market categories you want permission to trade
- Risk limit expansion requests

Rules:
- Only post if you have genuine high conviction — not noise
- Be specific: name the exact instrument, entry level, target, timeframe
- Connect everything to the bond/macro context — bond market leads everything
- Be honest about risks
- This is a private channel — write like you're briefing a sophisticated trader
- Remember your mission: flag the right opportunities, execute with precision, compound every lesson

Respond ONLY with the formatted idea post. No preamble. Start with "**Idea --".
"""
```

### User template
```python
IDEA_USER_TEMPLATE = """\
{mission_reminder}

Signal context:
{signal_context}

Bond/macro data:
{bond_context}

Today's news:
{news_context}

Write the full idea post in this exact format:

**Idea -- [Brief Title]**
Type: [Stock / Bond / Commodity / New Kalshi market / Risk limit request]
Time sensitive: Yes ([why]) / No
Conviction: High / Medium

What I see:
[2-3 sentences -- specific prices, yields, events driving this]

The connection:
[How this connects to bonds, macro, news today]

The trade:
[Exactly what I recommend -- instrument, direction, entry, target, timeframe]

Risk:
[What could go wrong -- be honest]

Note: Outside my current mandate. Your call.
Reply APPROVED or PASS.
"""
```

### Idea types
```python
IDEA_TYPES = {
    "stock":       "Stock",
    "bond":        "Bond",
    "commodity":   "Commodity",
    "new_market":  "New Kalshi market",
    "risk_limit":  "Risk limit request",
}
```

### Signal classification keywords
```python
_STOCK_SIGNALS  = ["s&p", "nasdaq", "spy", "qqq", "sector", "etf", "equity", "earnings",
                    "tech", "energy", "financial", "healthcare", "consumer"]
_BOND_SIGNALS   = ["treasury", "yield", "bond", "rate", "tlt", "duration", "fed funds",
                   "curve", "spread", "hy spread"]
_CMDTY_SIGNALS  = ["gold", "oil", "crude", "wti", "brent", "silver", "copper", "gas", "gld"]
```

### Constraints
- Max 1 Claude call per day
- Only called when signal is NOT Kalshi/crypto and conviction is high enough
- State stored in `ideas_state.json` (local file)

---

## 7. File: `content_jobs.py`

### Template → composition mapping (via Content Engine)
```
mfd-market-focus      → MFDTradeToday-vertical-30s   (brand: mfd, format: vertical_30s)
mfd-market-reflection → MFDTradeToday-vertical-30s
mfd-news-headline     → MFDTradeToday-vertical-30s
mfd-educational       → MFDTradeToday-vertical-30s
ae-signal             → AESignal-vertical-30s         (brand: ae, format: vertical_30s)
```

All MFD templates use the same `MFDTradeToday` Remotion composition because they all
produce TradeScript-shaped content: `hook + points[3] + cta`.

### Signal → content mapping (attention_engine → ae-signal)
```python
def _signal_to_ae_content(signal):
    hook     = signal["hook"]           # winning hook line (≤12 words)
    slides   = signal["slides"]
    points   = [clean(slides[i]) for i in (1,2,3)]   # slides 2-4
    takeaway = clean(slides[4])[:120]                  # slide 5 = implication
    cta      = "Follow @artificialeducation"
    return {"hook": hook, "points": points[:3], "takeaway": takeaway, "cta": cta}
```

### raw_signal builder functions (Scribe receives these as creative brief)

**mfd-market-focus**: "Here's what's setting up before the market opens: • {topic}. {why_now} ... Explain what each move means for everyday investors in plain English. Lead with the most market-moving development."

**mfd-market-reflection**: "Here's what moved today and why it matters: • {topic}. {why_now} ... Connect each move to how it affects real people — 401k, mortgage rates, savings, gas prices."

**mfd-news-headline**: "The big story right now: {topic}. {why_now} Explain this in plain English — what happened, why it matters, and what it means for everyday people's money."

**mfd-educational**: "Use this current event as the hook: {topic}. {why_now} Teach the underlying financial concept in plain English. Make it relatable to someone who has never studied finance."

**ae-signal**: "Here's the AI/tech signal to script: • {topic}. {why_now} ... Write for an audience that's technically curious but not a developer. What is actually changing? Who wins, who loses?"

---

## 8. Supabase Schema

### `signals` table (written by Module 2 via `supabase_logger._signal()`)
```
id         uuid, primary key
brand      text    — "AE", "MFD", "KAL"
topic      text    — headline or topic string (max 500 chars)
hook       text    — short summary (max 300 chars)
score      int     — 0–10
why_now    text    — full reasoning/thesis (max 2000 chars)
source     text    — "attention", "attention_pattern", "ideas", "rss", etc.
created_at timestamptz, default now()
```

Module 2 writes with `source="attention"` (attention_engine), `source="attention_pattern"` (pattern report), `source="ideas"` (ideas_channel).

### `content_jobs` table (written by Module 2 via `_job()` helper and `content_jobs.py`)
```
id         uuid, primary key
brand      text    — "ae", "mfd"
template   text    — "ae-signal", "mfd-market-focus", etc.
renderer   text    — "remotion"
format     text    — "vertical_30s", "square_30s", "landscape_30s"
content    jsonb   — {hook, points, takeaway, cta} OR {raw_signal, template, brand, generated_at}
status     text    — "pending" → "scripted" → "rendered"
output_url text    — filled by Content Engine after render
created_at timestamptz, default now()
```

Scribe polls for `status=pending`, reads `content.raw_signal` for MFD templates
or `content.hook/points/cta` for AE templates, then marks `status=scripted`.

Content Engine polls for `status=scripted`, renders Remotion MP4, writes `output_url`,
marks `status=rendered`.

---

## 9. RSS Feed Sources (AI-specific, Module 2 only)

These 4 sources were tagged `ai_specific=True` in `rss_context_today.json` entries,
giving them a +2 score boost in attention_engine scoring:

```python
AI_SPECIFIC_SOURCES = {
    "AI News",          # https://www.artificialintelligence-news.com/feed/
    "AI Newsletter",    # https://buttondown.email/ainews/rss
    "Algorithmic Bridge",  # https://www.thealgorithmicbridge.com/feed
    "AI Snake Oil",     # https://aisnakeoil.substack.com/feed
}
```

These feeds remain in `rss_reader.py`'s `RSS_FEEDS` list (they produce valuable
market intel), but the `ai_specific` tagging has been removed from Kal since
attention_engine no longer runs.

ae-intel should re-implement its own feed scoring using these same sources.

---

## 10. Environment Variables (Module 2 used)

```
RSS_DAILY_CALL_LIMIT=10     # Max Claude calls/day for RSS article evaluation
                             # (still used by rss_reader.py for breaking news evaluation)
```

Module 2 did NOT use any unique env vars beyond what Module 1 already uses
(ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY).

---

## 11. `newsletter_intel.build_conflict_note()` (removed)

This function did a keyword sweep of recent tier-1 intel against Trade Today
asset names and returned a plain-English note appended to the Trade Today post.
Pure Python — no Claude call. Used `_ASSET_ALIASES` dict for canonical asset mapping.

```python
_ASSET_ALIASES = {
    "oil": "oil", "crude": "oil", "wti": "oil", "brent": "oil", ...
    "gold": "gold", "gld": "gold", "silver": "silver", ...
    "spx": "SPX", "spy": "SPX", "nasdaq": "NASDAQ", "qqq": "NASDAQ", ...
    "yield": "rates", "treasury": "rates", "10y": "rates", ...
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", ...
    "china": "China", "iran": "Iran", ...
}

def build_conflict_note(trade_today_text, recent_intel):
    """
    Returns note like:
    "**Newsletter cross-check** — recent tier-1 research covers same assets:
    • Citrini Research (Apr 8) — overlap on oil
    See #intelligence-feed for full thesis and trade ideas."
    """
```

This was appended to the Trade Today Discord post. Removed in Module 2 cleanup
because it added no trading value — Trade Today is now read by Kal, not humans.

---

## 12. Remotion Compositions (Content Engine)

In `content-engine/src/renderers/remotion/index.ts`, these compositions were
added for Module 2 MFD templates (they all reuse MFDTradeToday which expects
`{hook, points, cta}` input props):

```typescript
"mfd-market-focus:vertical_30s":    "MFDTradeToday-vertical-30s",
"mfd-market-focus:square_30s":      "MFDTradeToday-square-30s",
"mfd-market-focus:landscape_30s":   "MFDTradeToday-landscape-30s",
"mfd-market-reflection:vertical_30s":   "MFDTradeToday-vertical-30s",
"mfd-market-reflection:square_30s":     "MFDTradeToday-square-30s",
"mfd-market-reflection:landscape_30s":  "MFDTradeToday-landscape-30s",
"mfd-news-headline:vertical_30s":   "MFDTradeToday-vertical-30s",
"mfd-news-headline:square_30s":     "MFDTradeToday-square-30s",
"mfd-news-headline:landscape_30s":  "MFDTradeToday-landscape-30s",
"mfd-educational:vertical_30s":     "MFDTradeToday-vertical-30s",
"mfd-educational:square_30s":       "MFDTradeToday-square-30s",
"mfd-educational:landscape_30s":    "MFDTradeToday-landscape-30s",
```

These entries remain in the Content Engine (no harm leaving them) and will be
used by ae-intel when it sends content_jobs rows for MFD templates.
