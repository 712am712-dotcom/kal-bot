"""
rss_reader.py — RSS feed intelligence for Kal.

Checks 11 financial RSS feeds every 15 minutes during market hours (6am–8pm ET)
and every 60 minutes overnight. A keyword pre-filter rejects ~80%+ of articles
without a Claude call. High-priority articles get ONE Claude call to evaluate
market impact and routing.

Output routing:
  #breaking         — major market-moving events (shared 5/day cap with Axios)
  #attention        — market intel notes (routed via market-pulse alias)
  rss_context_today.json — rolling context file for morning brief
  Supabase rss_articles  — full processing log

Daily limits:
  RSS_DAILY_CALL_LIMIT   — max Claude calls (default: 10, set via RSS_DAILY_CALL_LIMIT env var)
  Breaking news cap      — shared with Axios alerts (MAX_BREAKING_PER_DAY = 5)

Dedup: processed URLs tracked in memory, reset daily at midnight ET.
Context file: reset daily at midnight ET, max 40 articles.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ── Feed definitions ──────────────────────────────────────────────────────────

RSS_FEEDS: list[tuple[str, str]] = [
    # Financial / Macro (Reuters blocked on Railway — replaced with open alternatives)
    ("NYT Business",      "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("NPR Business",      "https://feeds.npr.org/1006/rss.xml"),
    ("MarketWatch",       "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Investing.com",     "https://www.investing.com/rss/news.rss"),
    ("WSJ Markets",       "https://www.wsj.com/xml/rss/3_7085.xml"),
    ("Dow Jones Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("ZeroHedge",         "https://zerohedge.com/fullrss2.xml"),
    # Crypto
    ("CoinTelegraph",     "https://cointelegraph.com/rss"),
    ("CoinDesk",          "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt",           "https://decrypt.co/feed"),
    # Macro analysis
    ("Kobeissi Letter",   "https://thekobeissiletter.substack.com/feed"),
    # AI-specific (auto-pass keyword filter, +3 signal score)
    ("AI News",              "https://www.artificialintelligence-news.com/feed/"),
    ("AI Newsletter",        "https://buttondown.email/ainews/rss"),
    ("Algorithmic Bridge",   "https://www.thealgorithmicbridge.com/feed"),
    ("AI Snake Oil",         "https://aisnakeoil.substack.com/feed"),
    # Tech / general (high volume, broad AI + tech coverage)
    ("TechCrunch",           "https://feeds.feedburner.com/techcrunch"),
    ("Hacker News",          "https://hnrss.org/frontpage"),
    ("The Verge",            "https://feeds.feedburner.com/TheVerge"),
]

# Sources that automatically pass the keyword filter and receive +3 signal score.
# Articles from these sources get ai_specific=True in the context file.
AI_SPECIFIC_SOURCES: set[str] = {
    "AI News",
    "AI Newsletter",
    "Algorithmic Bridge",
    "AI Snake Oil",
}


# ── Keyword filter lists ──────────────────────────────────────────────────────

HIGH_PRIORITY_KEYWORDS: list[str] = [
    # Fed / monetary policy
    "federal reserve", "fed reserve", " fed ", "fomc", "rate hike", "rate cut",
    "rate decision", "rate pause", "rate hold", "interest rate", "monetary policy",
    "quantitative easing", "quantitative tightening", " qe ", " qt ",
    # Economic data
    "inflation", " cpi ", " ppi ", " gdp ", "recession", "stagflation",
    "unemployment", "jobs report", "nonfarm payroll",
    # Yields / credit
    "treasury yield", "10-year yield", "10 year yield", "2-year yield",
    "2 year yield", "yield curve", "bond yield", "credit spread",
    "high yield spread", "junk bond",
    # Geopolitical / commodity
    " oil ", "crude oil", "opec", " iran ", " war ", "ceasefire", "sanctions",
    "tariff", "trade war", " china ", " russia ", "ukraine", "middle east",
    # Equity markets
    "s&p 500", "s&p500", " nasdaq ", "dow jones", "stock market",
    "market correction", "market crash", "market rally", "selloff", "sell-off",
    "bear market", "bull run", "circuit breaker", "volatility index", " vix ",
    # Crypto
    "bitcoin", " btc ", "ethereum", " eth ", "solana", " sol ", "crypto",
    "cryptocurrency", "defi", "stablecoin", "crypto regulation", "sec crypto",
    "coinbase", "binance", "crypto exchange",
    # Corporate / deal flow
    "merger", "acquisition", "takeover", " ipo ", "bankruptcy", "default",
    "earnings", "revenue miss", "revenue beat", "layoffs", "mass layoff",
    "debt ceiling", "bailout", "rescue",
]

LOW_PRIORITY_KEYWORDS: list[str] = [
    "sports", "football", "basketball", "soccer", "baseball", "nfl", "nba", "mlb",
    "celebrity", "entertainment", "lifestyle", "travel", "food", "recipe",
    "fashion", "music concert", "movie review", "tv show", "award show",
    "oscars", "grammys", "emmys",
]


# ── Module-level state ────────────────────────────────────────────────────────

_processed_urls:  set[str] = set()
_processed_date:  str      = ""
_rss_call_count:  int      = 0
_rss_call_date:   str      = ""


# ── File paths ────────────────────────────────────────────────────────────────

_BOT_DIR               = Path(__file__).parent
RSS_CONTEXT_PATH       = _BOT_DIR / "rss_context_today.json"
RSS_CONTEXT_YESTERDAY  = _BOT_DIR / "rss_context_yesterday.json"
RSS_CONTEXT_MAX        = 40   # max articles stored in rolling context file


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today_et() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date().isoformat()


def _now_et_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except (ImportError, KeyError):
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M ET")


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;",  "&", text)
    text = re.sub(r"&lt;",   "<", text)
    text = re.sub(r"&gt;",   ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+",    " ", text)
    return text.strip()


def _reset_daily_state() -> None:
    """Reset per-day tracking at midnight ET. Saves today's context as yesterday before clearing."""
    global _processed_urls, _processed_date, _rss_call_count, _rss_call_date
    today = _today_et()
    if _processed_date != today:
        # Save today's context as yesterday before the new day starts
        try:
            if RSS_CONTEXT_PATH.exists():
                import shutil
                shutil.copy2(RSS_CONTEXT_PATH, RSS_CONTEXT_YESTERDAY)
        except Exception as exc:
            log.debug("[rss] failed to save yesterday context: %s", exc)
        _processed_urls = set()
        _processed_date = today
        log.debug("[rss] daily URL dedup set reset for %s", today)
    if _rss_call_date != today:
        _rss_call_count = 0
        _rss_call_date  = today
        log.debug("[rss] daily Claude call counter reset for %s", today)


# ── RSS / Atom parser ─────────────────────────────────────────────────────────

_ATOM_NS = "http://www.w3.org/2005/Atom"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


def _parse_feed(xml_text: str) -> list[dict]:
    """
    Parse RSS 2.0 or Atom XML into a list of article dicts.
    Returns: [{title, url, description, pub_date}]
    Silently skips malformed entries.
    """
    articles: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.debug("[rss] xml parse error: %s", exc)
        return articles

    # Detect feed type by root tag (strip namespace prefix if present)
    root_tag = root.tag.split("}", 1)[-1] if "}" in root.tag else root.tag

    if root_tag == "rss":
        # ── RSS 2.0 ──────────────────────────────────────────────────────────
        channel = root.find("channel")
        if channel is None:
            return articles
        for item in channel.findall("item"):
            title = _strip_html(item.findtext("title") or "")
            link  = (item.findtext("link") or "").strip()
            # Fallback: permalink guid
            if not link:
                guid = item.find("guid")
                if guid is not None:
                    is_link = guid.get("isPermaLink", "true").lower() != "false"
                    if is_link and guid.text:
                        link = guid.text.strip()
            # Description — prefer content:encoded for richer text
            desc = ""
            content_enc = item.find(f"{{{_CONTENT_NS}}}encoded")
            if content_enc is not None and content_enc.text:
                desc = _strip_html(content_enc.text)
            if not desc:
                desc = _strip_html(item.findtext("description") or "")
            pub = item.findtext("pubDate") or ""
            if title and link:
                articles.append({
                    "title":       title[:250],
                    "url":         link,
                    "description": desc[:400],
                    "pub_date":    pub.strip(),
                })

    elif root_tag == "feed":
        # ── Atom ─────────────────────────────────────────────────────────────
        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            title = _strip_html(entry.findtext(f"{{{_ATOM_NS}}}title") or "")
            # Atom <link> can be an element with href attribute
            link = ""
            for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
                rel  = link_el.get("rel", "alternate")
                href = link_el.get("href", "")
                if href and rel in ("alternate", ""):
                    link = href
                    break
            if not link:
                link_el = entry.find(f"{{{_ATOM_NS}}}link")
                if link_el is not None:
                    link = link_el.get("href", "")
            # Summary or content
            desc = ""
            content_el = entry.find(f"{{{_ATOM_NS}}}content")
            if content_el is not None and content_el.text:
                desc = _strip_html(content_el.text)
            if not desc:
                desc = _strip_html(entry.findtext(f"{{{_ATOM_NS}}}summary") or "")
            pub = (
                entry.findtext(f"{{{_ATOM_NS}}}published")
                or entry.findtext(f"{{{_ATOM_NS}}}updated")
                or ""
            )
            if title and link:
                articles.append({
                    "title":       title[:250],
                    "url":         link.strip(),
                    "description": desc[:400],
                    "pub_date":    pub.strip(),
                })

    return articles


# ── Context file ──────────────────────────────────────────────────────────────

def _load_context() -> dict:
    """Load today's RSS context file. Returns empty structure if missing or stale."""
    today = _today_et()
    try:
        data = json.loads(RSS_CONTEXT_PATH.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return data
    except Exception:
        pass
    return {"date": today, "articles": []}


def _save_context(entry: dict) -> None:
    """Append an article entry to today's context file (capped at RSS_CONTEXT_MAX)."""
    ctx = _load_context()
    ctx["articles"].append(entry)
    ctx["articles"] = ctx["articles"][-RSS_CONTEXT_MAX:]
    try:
        RSS_CONTEXT_PATH.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("[rss] context file write failed: %s", exc)


def load_rss_context_for_brief() -> str:
    """
    Read today's RSS context file and return a formatted text block
    for inclusion in the morning brief prompt.
    Returns empty string if no articles available.
    """
    ctx = _load_context()
    articles = ctx.get("articles", [])
    if not articles:
        return ""

    lines = [
        "RSS INTELLIGENCE — today's additional market-relevant headlines "
        "(supplement to newsletters, de-duplicate overlapping stories):",
    ]
    seen: set[str] = set()
    for a in articles:
        headline = (a.get("headline") or a.get("title") or "").strip()
        if not headline or headline.lower() in seen:
            continue
        seen.add(headline.lower())
        source = a.get("source", "")
        angle  = a.get("market_angle", "").strip()
        ts     = a.get("timestamp", "")
        line   = f"- [{source}] {headline}"
        if angle:
            line += f" → {angle}"
        lines.append(line)

    if len(lines) == 1:   # header only, no usable articles
        return ""

    return "\n".join(lines)


# ── RSSReader class ───────────────────────────────────────────────────────────

class RSSReader:
    """
    Fetches RSS feeds, keyword-filters articles, evaluates high-priority items
    with Claude, routes to Discord or context file, logs everything to Supabase.
    """

    def __init__(
        self,
        anthropic_api_key: str,
        claude_model: str,
        supabase_url: str  = "",
        supabase_key: str  = "",
        call_limit:   int  = 10,
    ) -> None:
        self._api_key    = anthropic_api_key
        self._model      = claude_model
        self._sb_url     = supabase_url
        self._sb_key     = supabase_key
        self._call_limit = call_limit

    # ── Feed fetching ─────────────────────────────────────────────────────────

    async def _fetch_feed(self, source_name: str, url: str) -> list[dict]:
        """Fetch and parse one RSS/Atom feed. Returns article list (empty on error)."""
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; KalBot/1.0; market-intelligence)",
            "Accept":     "application/rss+xml, application/atom+xml, text/xml, */*",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                r = await c.get(url, headers=headers)
            if r.status_code != 200:
                log.debug("[rss] %s HTTP %d", source_name, r.status_code)
                return []
            articles = _parse_feed(r.text)
            log.debug("[rss] %s: %d articles", source_name, len(articles))
            return articles
        except Exception as exc:
            log.warning("[rss] %s fetch failed: %s", source_name, exc)
            return []

    # ── Keyword pre-filter ────────────────────────────────────────────────────

    @staticmethod
    def _keyword_priority(title: str, description: str) -> str:
        """
        Returns:
          'high'    — evaluate with Claude
          'context' — save to context file, no Claude call
          'skip'    — Supabase log only
        """
        text = (title + " " + description).lower()

        # Hard skip: non-market content even from financial feeds
        for kw in LOW_PRIORITY_KEYWORDS:
            if kw in text:
                return "skip"

        # High priority: genuinely market-moving keyword present
        for kw in HIGH_PRIORITY_KEYWORDS:
            if kw in text:
                return "high"

        # Everything else from financial feeds: save to context, no Claude
        return "context"

    # ── Claude evaluation ─────────────────────────────────────────────────────

    async def _evaluate_article(
        self,
        article: dict,
        kalshi_markets: list[dict],
        model_override: str | None = None,
    ) -> dict | None:
        """
        One Claude call per high-priority article.
        Returns evaluation dict (with 'cost' key) or None on failure/budget exhaustion.
        """
        global _rss_call_count, _rss_call_date
        _reset_daily_state()

        if _rss_call_count >= self._call_limit:
            log.debug("[rss] daily budget exhausted (%d/%d calls)", _rss_call_count, self._call_limit)
            return None

        # Top 5 Kalshi markets for routing context
        sorted_m = sorted(
            kalshi_markets,
            key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
            reverse=True,
        )[:5]
        kalshi_block = "\n".join(
            f"- {(m.get('title') or '?')[:60]} — "
            f"{round(float(m.get('yes_ask_dollars', 0.5) or 0.5) * 100)}%"
            for m in sorted_m
        ) or "No markets loaded"

        title  = article.get("title", "")
        desc   = article.get("description", "")
        source = article.get("source", "")

        system = (
            "You are a financial market analyst. Evaluate whether a news article "
            "is genuinely market-moving. Be fast and decisive. "
            "Respond ONLY with valid JSON — no preamble, no markdown, no code fences."
        )
        prompt = f"""Source: {source}
Headline: {title}
Summary: {desc[:300]}

Top Kalshi markets right now:
{kalshi_block}

Respond with ONLY this JSON:
{{
  "market_moving": true/false,
  "channel": "breaking" | "attention" | "context-only" | "skip",
  "headline": "plain English headline under 120 chars",
  "market_angle": "one sentence on the trading implication, or empty string",
  "kalshi_angle": "specific Kalshi market this could affect, or empty string"
}}

Routing rules:
- "breaking": genuinely market-moving event that could move prices TODAY
  (rate decisions, geopolitical shocks, major M&A, SEC actions, exchange failures)
- "attention": crypto-specific news, DeFi/exchange news, crypto regulatory moves
- "context-only": useful background but not immediately actionable (previews, summaries, analyst notes)
- "skip": not market-relevant despite matching keywords
- market_moving=true only if this could move crypto, stocks, bonds, or commodities within 24h"""

        try:
            import anthropic
            # RSS eval is a filtering/routing task — Sonnet is sufficient, Opus is overkill
            model  = model_override or "claude-sonnet-4-6"
            client = anthropic.Anthropic(api_key=self._api_key)
            resp   = client.messages.create(
                model=model,
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            raw  = resp.content[0].text.strip()
            # Strip any code fences the model may have added
            raw  = re.sub(r"^```[^\n]*\n?", "", raw)
            raw  = re.sub(r"\n?```$",       "", raw.strip())

            inp  = resp.usage.input_tokens
            out  = resp.usage.output_tokens
            cost = inp * 0.000003 + out * 0.000015   # Sonnet pricing

            result        = json.loads(raw)
            result["cost"] = round(cost, 6)

            _rss_call_count += 1
            _rss_call_date   = _today_et()
            log.info(
                "[rss] evaluated %r → channel=%s cost=$%.4f calls=%d/%d",
                title[:60], result.get("channel"), cost,
                _rss_call_count, self._call_limit,
            )
            return result

        except json.JSONDecodeError as exc:
            log.warning("[rss] json parse error evaluating %r: %s", title[:60], exc)
            _rss_call_count += 1  # still counts — we made the call
            _rss_call_date   = _today_et()
            return None
        except Exception as exc:
            log.warning("[rss] claude call failed for %r: %s", title[:60], exc)
            return None

    # ── Discord posting ───────────────────────────────────────────────────────

    @staticmethod
    async def _post_to_discord(
        channel: str,
        article: dict,
        eval_result: dict,
    ) -> None:
        """Route evaluated article to the appropriate Discord channel."""
        import discord_notifier as discord

        headline     = (eval_result.get("headline") or article.get("title", "")).strip()
        source       = article.get("source", "")
        market_angle = (eval_result.get("market_angle") or "").strip()
        kalshi_angle = (eval_result.get("kalshi_angle") or "").strip()
        url          = article.get("url", "")

        lines = [f"**{headline}**", f"_via {source}_"]
        if market_angle:
            lines += ["", f"**Trade angle:** {market_angle}"]
        if kalshi_angle:
            lines.append(f"**Kalshi:** {kalshi_angle}")
        if url:
            lines.append(f"<{url}>")
        post = "\n".join(lines)

        if channel == "breaking":
            await discord.notify_breaking_alert(post, source=source)
        elif channel in ("attention", "market-pulse"):
            await discord.notify_rss_intel(post)

    # ── Supabase logging ──────────────────────────────────────────────────────

    async def _log_to_supabase(
        self,
        article: dict,
        priority: str,
        eval_result: dict | None = None,
    ) -> None:
        """Log article processing to the rss_articles table."""
        if not self._sb_url or not self._sb_key:
            return
        row: dict[str, Any] = {
            "source":          article.get("source", "")[:100],
            "headline":        article.get("title",  "")[:500],
            "url":             article.get("url",    "")[:1000],
            "priority":        priority,
            "market_moving":   eval_result.get("market_moving") if eval_result else None,
            "discord_channel": eval_result.get("channel")       if eval_result else None,
            "claude_cost":     eval_result.get("cost")          if eval_result else None,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(
                    f"{self._sb_url}/rest/v1/rss_articles",
                    headers={
                        "apikey":        self._sb_key,
                        "Authorization": f"Bearer {self._sb_key}",
                        "Content-Type":  "application/json",
                        "Prefer":        "return=minimal",
                    },
                    json=row,
                )
        except Exception as exc:
            log.debug("[rss] supabase log failed: %s", exc)

    # ── Main check loop ───────────────────────────────────────────────────────

    async def check_all_feeds(
        self,
        kalshi_markets: list[dict] | None = None,
        model_override: str | None = None,
    ) -> int:
        """
        Fetch all feeds, evaluate new high-priority articles, route output.
        Returns count of new (not-yet-seen) articles found across all feeds.
        """
        if kalshi_markets is None:
            kalshi_markets = []
        global _processed_urls
        _reset_daily_state()

        # Import shared breaking counter from email_reader
        import email_reader as _er

        budget_exhausted = (_rss_call_count >= self._call_limit)
        new_count = 0

        for source_name, feed_url in RSS_FEEDS:
            articles = await self._fetch_feed(source_name, feed_url)
            await asyncio.sleep(0.5)  # gentle throttle between feeds

            for article in articles:
                url = article.get("url", "").strip()
                if not url or url in _processed_urls:
                    continue

                _processed_urls.add(url)
                article["source"] = source_name
                new_count += 1

                title    = article.get("title",       "")
                desc     = article.get("description", "")
                is_ai_specific = source_name in AI_SPECIFIC_SOURCES

                # All sources go through the keyword filter.
                # AI-specific sources get a score boost (+2) in attention_engine,
                # not an auto-pass here.
                priority = self._keyword_priority(title, desc)

                # ── Skip: non-market content ──────────────────────────────
                if priority == "skip":
                    asyncio.create_task(self._log_to_supabase(article, "skip"))
                    continue

                # ── Context only: save headline, no Claude call ───────────
                if priority == "context":
                    _save_context({
                        "timestamp":    _now_et_str(),
                        "source":       source_name,
                        "title":        title,
                        "headline":     title,
                        "market_angle": "",
                        "url":          url,
                        "ai_specific":  False,
                    })
                    asyncio.create_task(self._log_to_supabase(article, "context"))
                    continue

                # ── High priority: evaluate with Claude ───────────────────
                if budget_exhausted:
                    # Save to context file anyway — info is still useful for brief
                    log.debug("[rss] budget exhausted — %r saved to context only", title[:60])
                    _save_context({
                        "timestamp":    _now_et_str(),
                        "source":       source_name,
                        "title":        title,
                        "headline":     title,
                        "market_angle": "",
                        "url":          url,
                        "ai_specific":  is_ai_specific,
                    })
                    asyncio.create_task(self._log_to_supabase(article, "high_budget_exhausted"))
                    continue

                eval_result = await self._evaluate_article(
                    article, kalshi_markets, model_override=model_override
                )
                budget_exhausted = (_rss_call_count >= self._call_limit)

                if eval_result is None:
                    # Claude call failed — still save headline as context
                    _save_context({
                        "timestamp":    _now_et_str(),
                        "source":       source_name,
                        "title":        title,
                        "headline":     title,
                        "market_angle": "",
                        "url":          url,
                        "ai_specific":  is_ai_specific,
                    })
                    asyncio.create_task(self._log_to_supabase(article, "high_eval_failed"))
                    continue

                channel = eval_result.get("channel", "skip")

                # Save to context file for any non-skip result
                if channel != "skip":
                    _save_context({
                        "timestamp":    _now_et_str(),
                        "source":       source_name,
                        "title":        title,
                        "headline":     eval_result.get("headline") or title,
                        "market_angle": eval_result.get("market_angle", ""),
                        "url":          url,
                        "ai_specific":  is_ai_specific,
                    })

                # Post to Discord if warranted
                if channel in ("breaking", "attention"):
                    today = _today_et()

                    if channel == "breaking":
                        # Check shared breaking cap (same counter as Axios alerts)
                        breaking_today = (
                            _er._breaking_count if _er._breaking_date == today else 0
                        )
                        if breaking_today >= _er.MAX_BREAKING_PER_DAY:
                            log.debug("[rss] breaking cap reached — %r saved to context only", title[:60])
                            asyncio.create_task(
                                self._log_to_supabase(article, "high", eval_result)
                            )
                            continue

                    try:
                        await self._post_to_discord(channel, article, eval_result)
                        if channel == "breaking":
                            _er._breaking_count = (
                                (_er._breaking_count + 1) if _er._breaking_date == today else 1
                            )
                            _er._breaking_date = today
                        log.info("[rss] posted to #%s: %r", channel, title[:60])
                    except Exception as exc:
                        log.warning("[rss] discord post failed: %s", exc)

                asyncio.create_task(self._log_to_supabase(article, "high", eval_result))

        if new_count:
            log.info(
                "[rss] check complete: %d new articles, rss_calls=%d/%d",
                new_count, _rss_call_count, self._call_limit,
            )
        return new_count
