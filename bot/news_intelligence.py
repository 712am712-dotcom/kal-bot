"""
news_intelligence.py — Finnhub news + Kalshi market correlation engine.

Fetches news from Finnhub (free tier) and matches headlines against open
Kalshi markets to find potentially mispriced markets driven by current events.

When a strong match is found, ONE Claude call builds a full thesis.
Max 5 thesis calls per day to stay within cost budget.

Zero Claude calls for: news fetch, keyword extraction, market matching.
One Claude call per thesis (max 5/day).
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

FINNHUB_BASE = "https://finnhub.io/api/v1"
MAX_THESIS_PER_DAY = 5

# Keywords → Kalshi topic buckets for fast matching
KEYWORD_BUCKETS: dict[str, list[str]] = {
    "crypto":    ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "blockchain",
                  "defi", "coinbase", "binance", "sec crypto", "stablecoin"],
    "fed":       ["fed", "federal reserve", "interest rate", "fomc", "powell", "rate hike",
                  "rate cut", "basis points", "bps", "monetary policy", "inflation"],
    "oil":       ["oil", "crude", "opec", "energy", "petroleum", "brent", "wti", "barrel"],
    "war":       ["war", "military", "conflict", "attack", "missile", "troops", "invasion",
                  "sanctions", "nato", "pentagon", "defense"],
    "election":  ["election", "vote", "ballot", "president", "congress", "senate", "democrat",
                  "republican", "polling", "campaign"],
    "economy":   ["gdp", "recession", "unemployment", "jobs report", "cpi", "inflation",
                  "consumer price", "pce", "nonfarm", "payroll"],
    "tech":      ["nvidia", "apple", "microsoft", "google", "alphabet", "meta", "amazon",
                  "ai", "artificial intelligence", "earnings"],
    "banks":     ["bank", "banking", "fdic", "silicon valley bank", "credit", "lending",
                  "financial", "jpmorgan", "goldman"],
    "china":     ["china", "chinese", "beijing", "taiwan", "xi jinping", "trade war", "tariff"],
    "weather":   ["hurricane", "earthquake", "flood", "storm", "disaster", "emergency"],
}

# Kalshi market title keywords to topic buckets
KALSHI_TOPIC_MAP: dict[str, list[str]] = {
    "crypto":   ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"],
    "fed":      ["fed", "federal reserve", "rate", "fomc", "interest"],
    "oil":      ["oil", "crude", "brent", "wti", "barrel", "opec", "energy"],
    "war":      ["war", "conflict", "military", "armed", "nato", "iran", "russia", "ukraine"],
    "election": ["election", "president", "senate", "congress", "vote", "democrat", "republican"],
    "economy":  ["recession", "gdp", "unemployment", "cpi", "inflation", "jobs", "payroll"],
    "tech":     ["nvidia", "apple", "microsoft", "google", "meta", "amazon", "tech", "ai"],
    "banks":    ["bank", "banking", "credit", "financial"],
    "china":    ["china", "taiwan", "tariff", "trade"],
}


# ── Finnhub client ────────────────────────────────────────────────────────────

async def _finnhub_get(path: str, params: dict) -> Any:
    token = settings.finnhub_api_key
    if not token:
        return None
    params["token"] = token
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{FINNHUB_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_news(category: str = "general") -> list[dict]:
    """
    Fetch recent news from Finnhub.
    category: "general" | "forex" | "crypto" | "merger"
    Returns list of article dicts.
    """
    try:
        data = await _finnhub_get("/news", {"category": category})
        if not data:
            return []
        # Each article: {category, datetime, headline, id, image, related, source, summary, url}
        return data[:50]  # cap at 50 most recent
    except Exception as exc:
        log.warning("[news] fetch_news(%s) failed: %s", category, exc)
        return []


async def fetch_economic_calendar() -> list[dict]:
    """
    Fetch economic calendar events for the next 7 days from Finnhub.
    Returns list of event dicts.
    """
    try:
        today = datetime.date.today()
        end   = today + datetime.timedelta(days=7)
        data  = await _finnhub_get("/calendar/economic", {
            "from": today.isoformat(),
            "to":   end.isoformat(),
        })
        if not data:
            return []
        events = data.get("economicCalendar", [])
        # Sort by time, filter to high-impact only (impact >= 2)
        high_impact = [e for e in events if int(e.get("impact", 0) or 0) >= 2]
        high_impact.sort(key=lambda e: e.get("time", ""))
        return high_impact[:30]
    except Exception as exc:
        log.warning("[news] fetch_economic_calendar failed: %s", exc)
        return []


# ── Keyword extraction ────────────────────────────────────────────────────────

def extract_topics(text: str) -> set[str]:
    """
    Given a headline or summary, return the set of topic buckets it matches.
    """
    t = text.lower()
    matched = set()
    for topic, keywords in KEYWORD_BUCKETS.items():
        if any(kw in t for kw in keywords):
            matched.add(topic)
    return matched


def score_market_relevance(market_title: str, topics: set[str]) -> float:
    """
    Score how relevant a Kalshi market is to a set of topics.
    Returns 0.0 (no match) to 1.0 (strong match).
    """
    title = market_title.lower()
    score = 0.0
    for topic in topics:
        kalshi_kws = KALSHI_TOPIC_MAP.get(topic, [])
        matches = sum(1 for kw in kalshi_kws if kw in title)
        if matches > 0:
            score += min(matches * 0.4, 1.0)
    return min(score, 1.0)


# ── Market matching ───────────────────────────────────────────────────────────

def match_news_to_markets(
    articles: list[dict],
    kalshi_markets: list[dict],
    min_score: float = 0.4,
) -> list[dict]:
    """
    For each article, find relevant Kalshi markets.
    Returns list of match records: {article, markets, score, topics}.
    Only includes articles with at least one market scoring >= min_score.
    """
    results = []
    seen_headlines: set[str] = set()

    for article in articles:
        headline = article.get("headline", "")
        summary  = article.get("summary", "") or ""
        if not headline:
            continue
        # Deduplicate by headline
        key = headline[:80].lower()
        if key in seen_headlines:
            continue
        seen_headlines.add(key)

        text   = headline + " " + summary
        topics = extract_topics(text)
        if not topics:
            continue

        matched_markets = []
        for m in kalshi_markets:
            title  = m.get("title") or m.get("subtitle") or ""
            ticker = m.get("ticker", "")
            if not title:
                continue
            score = score_market_relevance(title, topics)
            if score >= min_score:
                vol_fp = m.get("volume_fp")
                volume = float(vol_fp) / 100.0 if vol_fp is not None else 0.0
                yes_ask = m.get("yes_ask_dollars")
                yes_price = float(yes_ask) if yes_ask is not None else 0.5
                matched_markets.append({
                    "ticker":    ticker,
                    "title":     title,
                    "yes_price": yes_price,
                    "volume":    volume,
                    "score":     round(score, 2),
                })

        if matched_markets:
            # Sort by score then volume
            matched_markets.sort(key=lambda x: (x["score"], x["volume"]), reverse=True)
            results.append({
                "article":  article,
                "markets":  matched_markets[:5],  # top 5 matches per article
                "topics":   list(topics),
                "score":    matched_markets[0]["score"],
            })

    # Sort by match score
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ── Thesis building (ONE Claude call) ────────────────────────────────────────

THESIS_PROMPT = """\
A news event has been matched to open Kalshi prediction markets.
Build a concise investment thesis.

News headline: {headline}
Summary: {summary}
Source: {source}

Matched Kalshi markets:
{markets_block}

Write a structured thesis in this EXACT format:

**What happened:** [1-2 sentences summarizing the news event]

**Related markets:**
{markets_placeholder}

**Historical context:** [1-2 sentences on how similar events have affected these markets historically]

**The edge:** [1-2 sentences on why these markets might be mispriced given this news]

**Confidence:** [High / Medium / Low]

**Note:** No trade placed — watching for confirmation
"""

_thesis_calls_today: int = 0
_thesis_calls_date: str  = ""


def _reset_thesis_counter_if_new_day() -> None:
    global _thesis_calls_today, _thesis_calls_date
    today = datetime.date.today().isoformat()
    if today != _thesis_calls_date:
        _thesis_calls_today = 0
        _thesis_calls_date  = today


async def build_thesis(match: dict, model_override: str | None = None) -> str | None:
    """
    Build a full thesis for a news-to-market match using ONE Claude call.
    Returns the thesis string, or None if limit reached or call fails.
    """
    global _thesis_calls_today

    _reset_thesis_counter_if_new_day()
    if _thesis_calls_today >= MAX_THESIS_PER_DAY:
        log.info("[news] thesis daily limit reached (%d)", MAX_THESIS_PER_DAY)
        return None

    article  = match["article"]
    markets  = match["markets"]
    headline = article.get("headline", "")
    summary  = (article.get("summary") or "")[:500]
    source   = article.get("source", "")

    markets_block = "\n".join(
        f"- {m['title']} (ticker: {m['ticker']}) — currently priced at {m['yes_price']:.0%}, volume ${m['volume']:,.0f}"
        for m in markets
    )
    markets_placeholder = "\n".join(
        f"• {m['title']} — currently priced at {m['yes_price']:.0%} — [assessment]"
        for m in markets
    )

    prompt = THESIS_PROMPT.format(
        headline=headline,
        summary=summary,
        source=source,
        markets_block=markets_block,
        markets_placeholder=markets_placeholder,
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model=settings.claude_model,
            max_tokens=800,
            system=(
                "You are Kal, a prediction market analyst. "
                "Write concise, actionable investment theses. "
                "Be specific about why a market might be mispriced. "
                "If you can't identify a genuine edge, say Confidence: Low."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        _thesis_calls_today += 1
        thesis = message.content[0].text.strip()
        log.info("[news] thesis built for: %s (calls today: %d)", headline[:60], _thesis_calls_today)
        return thesis
    except Exception as exc:
        log.warning("[news] thesis Claude call failed: %s", exc)
        return None


# ── NewsIntelligence orchestrator ─────────────────────────────────────────────

class NewsIntelligence:
    """
    Fetches news, matches to Kalshi markets, posts theses to Discord.
    Tracks which articles have already been processed to avoid duplicates.
    """

    def __init__(self, kalshi_client) -> None:
        self._kalshi = kalshi_client
        self._seen_article_ids: set[int] = set()
        self._last_calendar_post: str = ""  # date when calendar was last posted

    async def scan(self, post_thesis_fn, post_breaking_fn, post_calendar_fn) -> int:
        """
        Run one news intelligence scan.
        - post_thesis_fn(thesis, markets, headline): called when thesis is built
        - post_breaking_fn(article, markets): called for high-importance breaking news
        - post_calendar_fn(events, markets): called for morning calendar post
        Returns count of actions taken.
        """
        if not settings.finnhub_api_key:
            log.debug("[news] FINNHUB_API_KEY not set — skipping news scan")
            return 0

        actions = 0

        # Fetch open markets for matching (paginate up to 500 to keep it fast)
        try:
            resp = await self._kalshi._get("/markets", params={"status": "open", "limit": 200})
            markets = resp.get("markets", [])
        except Exception as exc:
            log.warning("[news] failed to fetch Kalshi markets for matching: %s", exc)
            markets = []

        # Fetch news
        articles: list[dict] = []
        for category in ("general", "crypto"):
            batch = await fetch_news(category)
            articles.extend(batch)

        # Deduplicate by article ID
        new_articles = [a for a in articles if a.get("id") not in self._seen_article_ids]
        for a in new_articles:
            if a.get("id"):
                self._seen_article_ids.add(a["id"])
        # Keep seen_ids from growing unboundedly
        if len(self._seen_article_ids) > 2000:
            self._seen_article_ids = set(list(self._seen_article_ids)[-1000:])

        if not new_articles:
            log.debug("[news] no new articles since last scan")
            return 0

        # Match news to markets
        matches = match_news_to_markets(new_articles, markets, min_score=0.4)
        log.info("[news] %d new articles → %d market matches", len(new_articles), len(matches))

        # Breaking news — flag immediately (no Claude, just post headline + markets)
        for article in new_articles:
            # Finnhub marks high-importance articles — we check for category "top"
            # or articles with at least 3 matched markets
            is_breaking = article.get("category", "") in ("top", "breaking")
            topics = extract_topics((article.get("headline") or "") + " " + (article.get("summary") or ""))
            matched = [m for m in matches if m["article"].get("id") == article.get("id")]
            if is_breaking and topics and matched:
                try:
                    await post_breaking_fn(article, matched[0]["markets"] if matched else [])
                    actions += 1
                except Exception as exc:
                    log.warning("[news] post_breaking failed: %s", exc)

        # Thesis — for top match if score is high enough
        strong_matches = [m for m in matches if m["score"] >= 0.7 and m["markets"]]
        for match in strong_matches[:2]:  # max 2 theses per scan
            try:
                thesis = await build_thesis(match)
                if thesis:
                    await post_thesis_fn(thesis, match["markets"], match["article"].get("headline", ""))
                    actions += 1
            except Exception as exc:
                log.warning("[news] thesis post failed: %s", exc)

        # Morning economic calendar (once per day)
        today = datetime.date.today().isoformat()
        now_hour = datetime.datetime.utcnow().hour
        if today != self._last_calendar_post and 12 <= now_hour <= 14:  # 8am ET ≈ 12-14 UTC
            try:
                events = await fetch_economic_calendar()
                if events:
                    await post_calendar_fn(events, markets)
                    self._last_calendar_post = today
                    actions += 1
            except Exception as exc:
                log.warning("[news] calendar post failed: %s", exc)

        return actions
