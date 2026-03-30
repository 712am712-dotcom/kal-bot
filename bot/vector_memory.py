"""
vector_memory.py — Searchable pattern library for Kal using Pinecone.

Replaces the shallow kal_lessons.json rolling 7-day log with a persistent,
queryable memory that compounds over time.

Embedding: lightweight character n-gram hashing (numpy only, no torch/ML libs).
  - Zero extra dependencies beyond numpy (already in requirements.txt)
  - Builds instantly on Railway — no 500MB model download
  - Good enough for matching similar Kalshi market patterns
  - Can be upgraded to proper embeddings later without changing the interface

Vector store: Pinecone free tier — index "kal-memory", 384 dims, cosine
Similarity:   cosine

Memory categories (stored as metadata):
  type:       trade_decision | trade_outcome | market_pattern | daily_lesson
  strategy:   fast_technical | conviction
  series:     KXGDP | KXFED | KXCPI | KXBTC15M | etc.
  action:     TRADE | SKIP | WATCH
  outcome:    WIN | LOSS | PENDING | unknown
  confidence: 0–100 (int)
  timestamp:  Unix epoch (int)

Graceful fallback: if Pinecone is not configured or fails, every method
is a no-op and get_relevant_context() returns "". The trading loop never crashes.
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# Vector dimension — must match the Pinecone index
_EMBED_DIM = 384

# How many similar memories to retrieve for context injection
_TOP_K = 4

# Minimum cosine similarity to include in context (avoids noise)
_MIN_SCORE = 0.60


def _series_from_ticker(ticker: str) -> str:
    """Extract the series prefix from a Kalshi ticker (e.g. 'KXGDP-...' → 'KXGDP')."""
    return ticker.split("-")[0].upper() if "-" in ticker else ticker.upper()[:10]


class VectorMemory:
    """
    Searchable vector memory for Kal. All methods are safe to call whether or
    not Pinecone is configured — they silently no-op on any error.

    Usage (in main.py):
        vm = VectorMemory()
        context = vm.get_relevant_context(ticker, strategy, crowd_price)
        # ... pass context into claude call ...
        vm.store_trade_memory(fields, analysis)

    Usage (on resolution):
        vm.store_outcome_memory(ticker, outcome="WIN", pnl=3.50)
    """

    def __init__(self) -> None:
        self._index     = None   # Pinecone index — lazy init
        self._ready     = False  # True once Pinecone is initialised without error
        self._attempted = False  # Avoid repeated init attempts after failure

    # ── Initialization ────────────────────────────────────────────────────────

    def _init(self) -> bool:
        """
        Lazy-initialise Pinecone index. Returns True if ready.
        Caches the result — only attempts once per process lifetime.
        """
        if self._ready:
            return True
        if self._attempted:
            return False

        self._attempted = True
        try:
            from config import settings

            api_key = getattr(settings, "pinecone_api_key", "")
            if not api_key:
                log.debug("vector_memory_unavailable reason=no_api_key using_fallback=True")
                return False

            from pinecone import Pinecone, ServerlessSpec
            pc = Pinecone(api_key=api_key)

            index_name = getattr(settings, "pinecone_index", "kal-memory")
            existing   = [i.name for i in pc.list_indexes()]

            if index_name not in existing:
                log.info("vector_memory_creating_index", name=index_name, dim=_EMBED_DIM)
                pc.create_index(
                    name=index_name,
                    dimension=_EMBED_DIM,
                    metric="cosine",
                    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
                )
                # Poll until ready (free tier usually takes 10–30s)
                for _ in range(15):
                    time.sleep(3)
                    try:
                        status = pc.describe_index(index_name).status
                        if status.get("ready", False):
                            break
                    except Exception:
                        pass

            self._index = pc.Index(index_name)
            self._ready = True
            log.info("vector_memory_initialized", index=index_name, embed="ngram_384")
            return True

        except Exception as exc:
            log.warning("vector_memory_pinecone_init_failed", error=str(exc)[:120])
            return False

    # ── Embedding — lightweight n-gram hashing (numpy only, no torch) ─────────

    def _embed(self, text: str) -> list[float]:
        """
        Deterministic 384-dim embedding using character n-gram hashing.

        Approach: hash each word + bigram into a bucket [0, 384), accumulate
        position-weighted counts, then L2-normalise. Produces stable vectors
        that capture vocabulary similarity without any ML library.

        Good enough for matching KXGDP vs KXGDP, WIN patterns vs WIN patterns,
        conviction vs fast_technical, etc. Can be swapped for proper embeddings
        later without changing any other code.
        """
        vec = np.zeros(_EMBED_DIM, dtype=np.float32)
        tokens = text.lower().split()

        for i, token in enumerate(tokens):
            # Unigram
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % _EMBED_DIM] += 1.0 / (i + 1)
            # Bigram with previous token
            if i > 0:
                bigram = tokens[i - 1] + "_" + token
                h2 = int(hashlib.sha1(bigram.encode()).hexdigest(), 16)
                vec[h2 % _EMBED_DIM] += 0.5 / (i + 1)

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def _uid(self, prefix: str, ticker: str) -> str:
        """Generate a unique, deterministic ID for a vector."""
        h = hashlib.sha1(f"{prefix}:{ticker}:{time.time_ns()}".encode()).hexdigest()[:12]
        return f"{prefix}_{h}"

    # ── Store methods ─────────────────────────────────────────────────────────

    def store_trade_memory(self, fields: dict, analysis) -> None:
        """
        Store a Sonnet analysis result as a searchable memory.
        Called after every ANALYZE → Sonnet decision.

        fields:   extract_market_fields() output dict
        analysis: MarketAnalysis dataclass
        """
        if not self._init():
            return
        try:
            ticker     = fields.get("ticker", "?")
            yes_price  = fields.get("yes_price", 0.5)
            volume     = fields.get("volume", 0.0)
            series     = _series_from_ticker(ticker)

            text = (
                f"Market {ticker} | {fields.get('title', '')} | "
                f"series={series} strategy={getattr(analysis, 'strategy', 'conviction')} "
                f"crowd={yes_price:.0%} kal_estimate={analysis.yes_probability:.0%} "
                f"confidence={analysis.confidence:.0%} edge={analysis.edge:+.0%} "
                f"action={analysis.recommendation} volume=${volume:,.0f} | "
                f"reasoning: {analysis.reasoning[:300]}"
            )

            meta = {
                "type":       "trade_decision",
                "ticker":     ticker,
                "series":     series,
                "strategy":   "fast_technical" if series.startswith(("KXBTC", "KXETH", "KXSOL")) else "conviction",
                "action":     "TRADE" if analysis.recommendation != "SKIP" else "SKIP",
                "outcome":    "PENDING",
                "confidence": int(analysis.confidence * 100),
                "crowd_pct":  int(yes_price * 100),
                "kal_pct":    int(analysis.yes_probability * 100),
                "timestamp":  int(time.time()),
                "text":       text[:512],
            }

            uid = self._uid("trade", ticker)
            self._index.upsert(vectors=[{"id": uid, "values": self._embed(text), "metadata": meta}])
            log.debug("vector_memory_stored_trade", uid=uid, ticker=ticker)

        except Exception as exc:
            log.warning("vector_memory_store_trade_failed", ticker=fields.get("ticker"), error=str(exc)[:80])

    def store_outcome_memory(self, ticker: str, outcome: str, pnl: float) -> None:
        """
        Store a trade outcome as a lesson memory.
        Called when a trade resolves WIN or LOSS.
        """
        if not self._init():
            return
        try:
            series = _series_from_ticker(ticker)
            text = (
                f"Trade outcome: {ticker} resolved {outcome} pnl=${pnl:+.2f} | "
                f"series={series} | "
                f"lesson: when trading {series} market, outcome was {outcome}"
            )
            meta = {
                "type":      "trade_outcome",
                "ticker":    ticker,
                "series":    series,
                "outcome":   outcome.upper(),
                "pnl":       round(pnl, 2),
                "timestamp": int(time.time()),
                "text":      text[:512],
            }
            uid = self._uid("outcome", ticker)
            self._index.upsert(vectors=[{"id": uid, "values": self._embed(text), "metadata": meta}])
            log.debug("vector_memory_stored_outcome", uid=uid, ticker=ticker, outcome=outcome)

        except Exception as exc:
            log.warning("vector_memory_store_outcome_failed", ticker=ticker, error=str(exc)[:80])

    def store_market_pattern(self, fields: dict, haiku_verdict: str, reason: str) -> None:
        """
        Store a market pattern observation — even for SKIP decisions.
        Called after every Haiku pre-filter verdict.
        """
        if not self._init():
            return
        try:
            ticker    = fields.get("ticker", "?")
            yes_price = fields.get("yes_price", 0.5)
            volume    = fields.get("volume", 0.0)
            series    = _series_from_ticker(ticker)

            text = (
                f"Pattern: {ticker} | {fields.get('title', '')} | "
                f"crowd={yes_price:.0%} volume=${volume:,.0f} "
                f"haiku_verdict={haiku_verdict} reason: {reason}"
            )
            meta = {
                "type":      "market_pattern",
                "ticker":    ticker,
                "series":    series,
                "action":    haiku_verdict,
                "outcome":   "unknown",
                "timestamp": int(time.time()),
                "text":      text[:512],
            }
            uid = self._uid("pattern", ticker)
            self._index.upsert(vectors=[{"id": uid, "values": self._embed(text), "metadata": meta}])

        except Exception as exc:
            log.warning("vector_memory_store_pattern_failed", ticker=ticker, error=str(exc)[:80])

    def store_daily_lesson(self, lesson_text: str) -> None:
        """
        Store a daily lesson/reflection. Replaces kal_lessons.json for future
        context retrieval. The JSON file is still written for backward compat.
        """
        if not self._init():
            return
        try:
            import datetime
            date = datetime.date.today().isoformat()
            text = f"Daily lesson {date}: {lesson_text}"
            meta = {
                "type":      "daily_lesson",
                "series":    "ALL",
                "outcome":   "unknown",
                "timestamp": int(time.time()),
                "text":      text[:512],
            }
            uid = f"lesson_{date}"   # one per day, overwrites previous
            self._index.upsert(vectors=[{"id": uid, "values": self._embed(text), "metadata": meta}])
            log.debug("vector_memory_stored_daily_lesson", date=date)

        except Exception as exc:
            log.warning("vector_memory_store_lesson_failed", error=str(exc)[:80])

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def search_similar_patterns(
        self,
        query_text: str,
        top_k: int = _TOP_K,
        filter_meta: dict | None = None,
    ) -> list[dict]:
        """
        Search for the most similar past memories.
        Returns list of {"score": float, "text": str, "metadata": dict}.
        Returns [] on any error or when Pinecone is unavailable.
        """
        if not self._init():
            return []
        try:
            query_vec = self._embed(query_text)
            kwargs: dict = {"vector": query_vec, "top_k": top_k, "include_metadata": True}
            if filter_meta:
                kwargs["filter"] = filter_meta
            results = self._index.query(**kwargs)
            matches = []
            for m in results.get("matches", []):
                score = m.get("score", 0.0)
                if score < _MIN_SCORE:
                    continue
                meta = m.get("metadata", {})
                matches.append({
                    "score":    round(score, 3),
                    "text":     meta.get("text", ""),
                    "metadata": meta,
                })
            return matches

        except Exception as exc:
            log.warning("vector_memory_search_failed", error=str(exc)[:80])
            return []

    def get_relevant_context(
        self,
        ticker: str,
        strategy: str,
        crowd_price: float,
        title: str = "",
    ) -> str:
        """
        Retrieve relevant past memories for a market before Sonnet analysis.
        Returns a formatted context string, or "" if nothing useful is found.

        Injected into the Sonnet prompt as:
          "── RELEVANT PAST PATTERNS ──\n{context}"
        """
        if not self._init():
            return ""
        try:
            series = _series_from_ticker(ticker)
            query  = (
                f"{series} market {title} crowd_price={crowd_price:.0%} "
                f"strategy={strategy}"
            )
            memories = self.search_similar_patterns(query, top_k=_TOP_K)
            if not memories:
                return ""

            lines = ["── RELEVANT PAST PATTERNS (from Kal's memory) ──"]
            for m in memories:
                score = m["score"]
                text  = m["text"]
                meta  = m["metadata"]
                outcome_tag = ""
                if meta.get("outcome") not in ("unknown", "PENDING", ""):
                    outcome_tag = f" [{meta['outcome']}]"
                lines.append(f"  [{score:.0%} match]{outcome_tag} {text[:200]}")

            context = "\n".join(lines)
            log.debug("vector_memory_context_retrieved", ticker=ticker, matches=len(memories))
            return context

        except Exception as exc:
            log.warning("vector_memory_context_failed", error=str(exc)[:80])
            return ""
