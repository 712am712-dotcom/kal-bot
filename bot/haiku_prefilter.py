"""
haiku_prefilter.py — Cheap Haiku pre-filter that runs before any Sonnet/Opus call.

Architecture:
  market → Haiku ($0.0001/call) → ANALYZE / SKIP / WATCH
                                    ↓
                              ANALYZE only → Sonnet ($0.06/call)

Expected savings: 80%+ of markets are SKIP → Sonnet call rate drops 5-10x.

Haiku model: claude-haiku-4-5-20251001
Estimated cost: ~$0.0001 per call (200 in + 50 out tokens)
Daily budget: ~$0.01 for 96 scan cycles × 8 markets = ~768 Haiku calls
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Haiku input/output token pricing (per million tokens)
_HAIKU_IN_PRICE  = 0.80   # $0.80 / 1M input tokens
_HAIKU_OUT_PRICE = 4.00   # $4.00 / 1M output tokens

# Watch list settings
_WATCH_MAX_CHECKS   = 3      # drop a market after this many WATCH verdicts
_WATCH_PRICE_DELTA  = 0.05   # promote to ANALYZE if crowd price moves >5%
_WATCH_TTL_HOURS    = 2      # drop from watch list after this many hours

Verdict = Literal["ANALYZE", "SKIP", "WATCH"]


@dataclass
class PrefilterResult:
    verdict:      Verdict
    reason:       str
    ticker:       str
    cost_dollars: float = 0.0
    input_tokens: int   = 0
    output_tokens: int  = 0
    latency_ms:   int   = 0


@dataclass
class _WatchEntry:
    first_seen:  float         # monotonic timestamp
    crowd_price: float
    volume:      float
    check_count: int = 0
    title:       str = ""


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a fast market pre-filter. Return only valid JSON — no markdown, "
    "no explanation outside the JSON object."
)

_PROMPT_TEMPLATE = """\
Kalshi prediction market pre-filter. Apply these EXACT rules, in order:

Ticker: {ticker}
Title: {title}
YES probability: {yes_pct}%
Volume: ${volume:,.0f}
Closes in: {minutes_to_close:.0f} minutes
Strategy: {strategy}

HARD RULES — apply mechanically, override everything else:
1. SKIP if YES% >= 92 OR YES% <= 8  (outcome decided, no edge)
2. SKIP if YES% >= 47 AND YES% <= 53  (coin flip zone, no edge)
3. WATCH if volume < 2000 AND closes in > 30 minutes  (volume still building)
4. WATCH if closes in < 5 minutes  (too late to act)
5. ANALYZE if volume >= 2000 AND closes in >= 5 minutes AND YES% outside 47-53 AND YES% outside 92-100 AND YES% outside 0-8

If none of the above triggered, default to ANALYZE.

Current values: YES%={yes_pct}, volume=${volume:,.0f}, closes_in={minutes_to_close:.0f}min

Return JSON only — no other text:
{{"verdict": "ANALYZE|SKIP|WATCH", "reason": "<one short sentence explaining which rule fired>"}}"""


class HaikuPrefilter:
    """
    Cheap Haiku pre-filter for Kalshi markets.
    Call screen() before every Sonnet analysis call.
    """

    def __init__(self) -> None:
        self._client = None          # lazy-init — avoid startup crash if key missing
        self._watch: dict[str, _WatchEntry] = {}

    def _get_client(self):
        if self._client is None:
            import anthropic
            from config import settings
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    # ── Core screen method ────────────────────────────────────────────────────

    def screen(
        self,
        fields: dict,
        strategy: str,
    ) -> PrefilterResult:
        """
        Run Haiku pre-filter on a market. Synchronous (claude_client.py is sync).
        Returns a PrefilterResult with verdict ANALYZE / SKIP / WATCH.
        """
        ticker      = fields.get("ticker", "?")
        yes_price   = fields.get("yes_price", 0.5)
        volume      = fields.get("volume", 0.0)
        close_time  = fields.get("close_time", "")

        # Compute minutes to close
        minutes_to_close = 9999.0
        if close_time:
            try:
                ct  = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                minutes_to_close = max(0.0, (ct - now).total_seconds() / 60)
            except Exception:
                pass

        prompt = _PROMPT_TEMPLATE.format(
            ticker=ticker,
            title=fields.get("title", "?")[:120],
            yes_pct=round(yes_price * 100, 1),
            volume=volume,
            minutes_to_close=minutes_to_close,
            strategy=strategy,
        )

        t0 = time.time()
        try:
            client = self._get_client()
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=80,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            latency_ms = int((time.time() - t0) * 1000)

            raw = msg.content[0].text.strip() if msg.content else "{}"
            # Strip markdown fences if Haiku adds them anyway
            raw = re.sub(r"```(?:json)?|```", "", raw).strip()

            parsed = json.loads(raw)
            verdict_raw = parsed.get("verdict", "SKIP").upper()
            if verdict_raw not in ("ANALYZE", "SKIP", "WATCH"):
                verdict_raw = "SKIP"
            verdict: Verdict = verdict_raw  # type: ignore[assignment]
            reason  = parsed.get("reason", "no reason")[:200]

            in_tok  = msg.usage.input_tokens  if msg.usage else 0
            out_tok = msg.usage.output_tokens if msg.usage else 0
            cost    = (in_tok * _HAIKU_IN_PRICE + out_tok * _HAIKU_OUT_PRICE) / 1_000_000

            log.info(
                "haiku_prefilter_result",
                ticker=ticker,
                verdict=verdict,
                reason=reason,
                cost=f"${cost:.5f}",
                latency_ms=latency_ms,
                in_tok=in_tok,
                out_tok=out_tok,
            )

            # Update watch list
            if verdict == "WATCH":
                self._add_to_watch(ticker, yes_price, volume, fields.get("title", ""))
            elif verdict == "ANALYZE":
                # Remove from watch list — it's being analyzed now
                self._watch.pop(ticker, None)

            return PrefilterResult(
                verdict=verdict,
                reason=reason,
                ticker=ticker,
                cost_dollars=cost,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
            )

        except Exception as exc:
            latency_ms = int((time.time() - t0) * 1000)
            log.warning(
                "haiku_prefilter_error",
                ticker=ticker,
                error=str(exc)[:120],
                latency_ms=latency_ms,
            )
            # On any error, default to ANALYZE so we don't silently drop markets
            return PrefilterResult(
                verdict="ANALYZE",
                reason=f"prefilter_error: {str(exc)[:60]}",
                ticker=ticker,
                latency_ms=latency_ms,
            )

    # ── Watch list management ─────────────────────────────────────────────────

    def _add_to_watch(self, ticker: str, crowd_price: float, volume: float, title: str) -> None:
        if ticker in self._watch:
            self._watch[ticker].check_count += 1
        else:
            self._watch[ticker] = _WatchEntry(
                first_seen=time.monotonic(),
                crowd_price=crowd_price,
                volume=volume,
                title=title,
            )

    def promote_watches(self, current_market_fields: list[dict]) -> list[str]:
        """
        Check each watched market against current prices.
        Returns list of tickers promoted to ANALYZE (crowd moved >5%).
        Also evicts stale/expired entries.

        Call once per scan cycle with the current market field list.
        """
        now        = time.monotonic()
        ttl_secs   = _WATCH_TTL_HOURS * 3600
        to_analyze: list[str] = []
        to_evict:   list[str] = []

        current_prices: dict[str, float] = {
            f["ticker"]: f["yes_price"]
            for f in current_market_fields
            if "ticker" in f and "yes_price" in f
        }

        for ticker, entry in self._watch.items():
            age_secs = now - entry.first_seen

            # Evict if too old or too many WATCH verdicts
            if age_secs > ttl_secs or entry.check_count > _WATCH_MAX_CHECKS:
                to_evict.append(ticker)
                log.debug("watch_evicted", ticker=ticker, checks=entry.check_count, age_min=int(age_secs/60))
                continue

            # Promote if crowd price moved significantly
            current_price = current_prices.get(ticker)
            if current_price is not None:
                delta = abs(current_price - entry.crowd_price)
                if delta >= _WATCH_PRICE_DELTA:
                    to_analyze.append(ticker)
                    to_evict.append(ticker)
                    log.info(
                        "watch_promoted",
                        ticker=ticker,
                        old_price=f"{entry.crowd_price:.1%}",
                        new_price=f"{current_price:.1%}",
                        delta=f"{delta:.1%}",
                    )

        for t in to_evict:
            self._watch.pop(t, None)

        return to_analyze

    def get_watch_list(self) -> list[dict]:
        """Return current watch list as list of dicts for Discord reporting."""
        now = time.monotonic()
        result = []
        for ticker, entry in self._watch.items():
            result.append({
                "ticker":      ticker,
                "title":       entry.title,
                "crowd_price": entry.crowd_price,
                "volume":      entry.volume,
                "checks":      entry.check_count,
                "age_min":     int((now - entry.first_seen) / 60),
            })
        return sorted(result, key=lambda x: x["age_min"])

    @property
    def watch_count(self) -> int:
        return len(self._watch)
