"""
financial_datasets_client.py — Financial Datasets API integration (Module 1).

Feeds pulled on each scan cycle:
  1. Price snapshots  — SPY, QQQ, GLD, USO, VXX, UUP (flag >3% daily move)
  2. Insider trades   — C-suite buys >$500k across a watchlist (SEC Form 4)
  3. SEC 8-K filings  — same-day 8-Ks across a watchlist
  4. Earnings feed    — global feed; flag surprise beats/misses >10%
  5. Economic events  — today + tomorrow HIGH-impact events (Forex Factory)

Signals are written to Supabase signals table with source=financial_datasets.

Scoring:
  Insider buy cluster (≥2 C-suite buys same ticker within 7 days): 9
  Single C-suite buy >$500k:                                        7
  8-K filed same day:                                               7
  Earnings EPS surprise >10%:                                       7
  Price move >3% same day:                                          6
  HIGH-impact macro event within 24h:                               5
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

FD_BASE = "https://api.financialdatasets.ai"

# ── Watchlists ────────────────────────────────────────────────────────────────

# US-listed ETFs covering the requested tickers (BTC/ETH handled by existing Kraken feed)
_PRICE_TICKERS = ["SPY", "QQQ", "GLD", "USO", "VXX", "UUP"]

# Tickers to scan for insider trades and same-day 8-K filings
_WATCHLIST = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX",
    "JPM", "BAC", "GS", "MS", "WFC",
    "XOM", "CVX",
    "V", "MA",
    "SPY", "QQQ",
]

# Roles considered "C-suite" for insider trade filtering
_CSUITE_KEYWORDS = [
    "ceo", "chief executive",
    "cfo", "chief financial",
    "coo", "chief operating",
    "cto", "chief technology",
    "president",
    "chairman",
    "director",
    "evp", "svp", "executive vice",
]

# Purchase-type transaction indicators (not sales/gifts/awards)
_PURCHASE_KEYWORDS = ["purchase", "open market", "acquisition", "buy"]
_SALE_KEYWORDS     = ["sale", "sell", "gift", "award", "exercise", "conversion"]

# Minimum C-suite buy value to flag
_INSIDER_BUY_MIN_VALUE = 500_000.0


def _fd_headers() -> dict[str, str]:
    return {"X-API-KEY": settings.financial_datasets_api_key}


def _is_csuite(title_or_role: str) -> bool:
    t = (title_or_role or "").lower()
    return any(k in t for k in _CSUITE_KEYWORDS)


def _is_purchase(transaction_type: str) -> bool:
    t = (transaction_type or "").lower()
    if any(k in t for k in _SALE_KEYWORDS):
        return False
    return any(k in t for k in _PURCHASE_KEYWORDS)


def _trade_value(trade: dict) -> float:
    """Return dollar value of a trade. Compute from shares × price if pre-calc is absent."""
    v = trade.get("value") or trade.get("total_value")
    if v:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    shares = trade.get("shares") or trade.get("shares_traded") or 0
    price  = trade.get("price") or trade.get("price_per_share") or 0
    try:
        return float(shares) * float(price)
    except (TypeError, ValueError):
        return 0.0


# ── Main scanner class ────────────────────────────────────────────────────────

class FinancialDatasetsScanner:
    """
    Async scanner that fetches all five feeds, scores each signal, and writes
    them to Supabase. Instantiate once; call `scan()` on each cycle.

    In-memory dedup prevents the same (signal_type, ticker) from firing
    multiple times within the same day.
    """

    def __init__(self) -> None:
        self._dedup: set[str] = set()   # "type:ticker:YYYY-MM-DD"
        self._dedup_date = ""           # date string when dedup was last reset

    def _reset_dedup_if_new_day(self) -> None:
        today = datetime.date.today().isoformat()
        if today != self._dedup_date:
            self._dedup.clear()
            self._dedup_date = today

    def _seen(self, sig_type: str, ticker: str) -> bool:
        key = f"{sig_type}:{ticker}:{self._dedup_date}"
        if key in self._dedup:
            return True
        self._dedup.add(key)
        return False

    async def scan(self) -> int:
        """
        Run one full scan of all five feeds.
        Returns number of signals written.
        """
        if not settings.financial_datasets_api_key:
            log.debug("[fd] FINANCIAL_DATASETS_API_KEY not set — skipping scan")
            return 0

        self._reset_dedup_if_new_day()

        (
            price_signals,
            insider_signals,
            filing_signals,
            earnings_signals,
            calendar_signals,
        ) = await asyncio.gather(
            self._scan_prices(),
            self._scan_insider_trades(),
            self._scan_8k_filings(),
            self._scan_earnings_feed(),
            self._scan_economic_calendar(),
            return_exceptions=True,
        )

        all_signals: list[dict] = []
        for result in (
            price_signals, insider_signals, filing_signals,
            earnings_signals, calendar_signals
        ):
            if isinstance(result, list):
                all_signals.extend(result)
            elif isinstance(result, Exception):
                log.warning("[fd] feed error: %s", result)

        count = 0
        for sig in all_signals:
            try:
                await _write_signal(sig)
                count += 1
            except Exception as exc:
                log.warning("[fd] signal write failed: %s", exc)

        log.info("[fd] scan complete — %d signals written", count)
        return count

    # ── Feed 1: Price snapshots ───────────────────────────────────────────────

    async def _scan_prices(self) -> list[dict]:
        signals: list[dict] = []
        snapshots = await _fetch_price_snapshots(_PRICE_TICKERS)

        for ticker, snap in snapshots.items():
            if not snap:
                continue

            open_px  = _safe_float(snap.get("open"))
            close_px = _safe_float(
                snap.get("close") or snap.get("last") or snap.get("price")
            )

            if not open_px or not close_px:
                continue

            pct = (close_px - open_px) / open_px * 100.0

            if abs(pct) >= 3.0:
                direction = "up" if pct > 0 else "down"
                topic = f"{ticker} {direction} {abs(pct):.1f}% today"
                hook  = f"{ticker} moved {pct:+.1f}% — flagged for content"

                if not self._seen("price_move", ticker):
                    signals.append({
                        "brand":   "MFD",
                        "topic":   topic,
                        "hook":    hook,
                        "score":   6,
                        "why_now": (
                            f"{ticker} open={open_px:.2f} close={close_px:.2f} "
                            f"change={pct:+.2f}%"
                        ),
                        "source": "financial_datasets",
                    })

        return signals

    # ── Feed 2: Insider trades ────────────────────────────────────────────────

    async def _scan_insider_trades(self) -> list[dict]:
        signals: list[dict] = []
        today     = datetime.date.today()
        week_ago  = (today - datetime.timedelta(days=7)).isoformat()
        today_str = today.isoformat()

        # Fetch recent insider trades for every watchlist ticker in parallel.
        # Keep concurrency moderate — 5 at a time to stay within rate limits.
        semaphore = asyncio.Semaphore(5)

        async def _fetch_ticker(ticker: str) -> tuple[str, list[dict]]:
            async with semaphore:
                return ticker, await _fetch_insider_trades(
                    ticker, filing_date_gte=week_ago
                )

        results = await asyncio.gather(
            *[_fetch_ticker(t) for t in _WATCHLIST],
            return_exceptions=True,
        )

        # Collect qualifying C-suite buys per ticker for cluster detection
        ticker_buys: dict[str, list[dict]] = {}  # ticker → list of buys
        for result in results:
            if isinstance(result, Exception):
                continue
            ticker, trades = result
            if not trades:
                continue

            qualifying: list[dict] = []
            for trade in trades:
                role  = (
                    trade.get("title") or trade.get("relationship")
                    or trade.get("insider_title") or ""
                )
                txn   = (
                    trade.get("transaction_type") or trade.get("type") or ""
                )
                value = _trade_value(trade)
                name  = trade.get("name") or trade.get("insider_name") or "Unknown"

                if not _is_csuite(role):
                    continue
                if not _is_purchase(txn):
                    continue
                if value < _INSIDER_BUY_MIN_VALUE:
                    continue

                qualifying.append({
                    "name":  name,
                    "role":  role,
                    "value": value,
                    "txn":   txn,
                    "date":  trade.get("filing_date") or today_str,
                })

            if qualifying:
                ticker_buys[ticker] = qualifying

        # Score: cluster (≥2) = 9, single buy = 7
        for ticker, buys in ticker_buys.items():
            is_cluster = len(buys) >= 2
            score      = 9 if is_cluster else 7
            total_val  = sum(b["value"] for b in buys)

            names_str = ", ".join(
                f"{b['name']} ({b['role'][:20]})" for b in buys[:3]
            )
            topic = (
                f"{'Cluster: ' if is_cluster else ''}C-suite buying {ticker} — "
                f"${total_val:,.0f} total"
            )
            hook = (
                f"{len(buys)} C-suite insider{'s' if len(buys) > 1 else ''} "
                f"buying {ticker} — ${total_val / 1_000_000:.1f}M"
            )
            why_now = f"Insiders: {names_str}. Total value: ${total_val:,.0f}"

            if not self._seen("insider_buy", ticker):
                signals.append({
                    "brand":   "MFD",
                    "topic":   topic,
                    "hook":    hook[:300],
                    "score":   score,
                    "why_now": why_now,
                    "source":  "financial_datasets",
                })

        return signals

    # ── Feed 3: SEC 8-K filings ────────────────────────────────────────────────

    async def _scan_8k_filings(self) -> list[dict]:
        signals: list[dict] = []
        today_str = datetime.date.today().isoformat()

        semaphore = asyncio.Semaphore(5)

        async def _fetch_ticker(ticker: str) -> tuple[str, list[dict]]:
            async with semaphore:
                return ticker, await _fetch_8k_filings(
                    ticker, filing_date_gte=today_str
                )

        results = await asyncio.gather(
            *[_fetch_ticker(t) for t in _WATCHLIST],
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                continue
            ticker, filings = result
            if not filings:
                continue

            for f in filings[:3]:  # cap per ticker to avoid spam
                filing_date = f.get("filing_date") or ""
                # Only same-day filings
                if not filing_date.startswith(today_str):
                    continue

                form_type   = f.get("form_type") or "8-K"
                description = f.get("description") or f.get("form_description") or ""
                filing_url  = f.get("filing_url") or f.get("url") or ""

                topic   = f"{ticker} filed {form_type} today"
                hook    = f"{ticker} {form_type} — {description[:80]}" if description else topic
                why_now = f"Filed {filing_date}. {filing_url}"

                if not self._seen("8k_filing", ticker):
                    signals.append({
                        "brand":   "MFD",
                        "topic":   topic,
                        "hook":    hook[:300],
                        "score":   7,
                        "why_now": why_now[:2000],
                        "source":  "financial_datasets",
                    })

        return signals

    # ── Feed 4: Earnings surprises ────────────────────────────────────────────

    async def _scan_earnings_feed(self) -> list[dict]:
        signals: list[dict] = []
        feed = await _fetch_earnings_feed(limit=50)

        today_str = datetime.date.today().isoformat()

        for entry in feed:
            ticker      = entry.get("ticker") or ""
            filing_date = entry.get("filing_date") or ""

            # Only consider earnings filed today or yesterday
            if not (today_str in filing_date or
                    (datetime.date.today() - datetime.timedelta(days=1)).isoformat() in filing_date):
                continue

            quarterly = entry.get("quarterly") or {}
            annual    = entry.get("annual") or {}
            period    = quarterly or annual
            if not period:
                continue

            eps_surprise = (period.get("eps_surprise") or "").upper()
            rev_surprise = (period.get("revenue_surprise") or "").upper()

            # Compute magnitude
            eps_actual   = _safe_float(period.get("earnings_per_share"))
            eps_estimate = _safe_float(period.get("estimated_earnings_per_share"))
            rev_actual   = _safe_float(period.get("revenue"))
            rev_estimate = _safe_float(period.get("estimated_revenue"))

            eps_pct = abs((eps_actual - eps_estimate) / eps_estimate * 100) \
                if eps_estimate and eps_estimate != 0 else 0.0
            rev_pct = abs((rev_actual - rev_estimate) / rev_estimate * 100) \
                if rev_estimate and rev_estimate != 0 else 0.0

            # Flag >10% beat or miss
            if max(eps_pct, rev_pct) < 10.0:
                continue
            if eps_surprise not in ("BEAT", "MISS") and rev_surprise not in ("BEAT", "MISS"):
                continue

            fiscal_period = entry.get("fiscal_period") or ""
            direction     = eps_surprise if eps_surprise in ("BEAT", "MISS") else rev_surprise
            magnitude     = max(eps_pct, rev_pct)

            topic   = f"{ticker} earnings {direction} {magnitude:.0f}% — {fiscal_period}"
            hook    = (
                f"{ticker} {direction.lower()}s by {magnitude:.0f}% — "
                f"EPS ${eps_actual:.2f} vs est ${eps_estimate:.2f}"
                if eps_estimate else
                f"{ticker} {direction.lower()}s estimates by {magnitude:.0f}%"
            )
            why_now = (
                f"Filed: {filing_date}. EPS={eps_actual} est={eps_estimate} "
                f"surprise={eps_surprise}. Rev={rev_actual} est={rev_estimate} "
                f"surprise={rev_surprise}."
            )

            if not self._seen("earnings", ticker):
                signals.append({
                    "brand":   "MFD",
                    "topic":   topic,
                    "hook":    hook[:300],
                    "score":   7,
                    "why_now": why_now[:2000],
                    "source":  "financial_datasets",
                })

        return signals

    # ── Feed 5: Economic calendar (Forex Factory) ─────────────────────────────

    async def _scan_economic_calendar(self) -> list[dict]:
        """
        Pull today + tomorrow HIGH-impact events from Forex Factory
        (free JSON feed, no legal restrictions on scraping).
        Event dicts have key "event" for the event name.
        """
        signals: list[dict] = []
        from economic_calendar import (
            get_today_high_impact_events,
            get_tomorrow_high_impact_events,
        )

        today_events, tomorrow_events = await asyncio.gather(
            get_today_high_impact_events(),
            get_tomorrow_high_impact_events(),
            return_exceptions=True,
        )

        today_str    = datetime.date.today().isoformat()
        tomorrow_str = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

        for event in (today_events if isinstance(today_events, list) else []):
            name = event.get("event") or event.get("title") or ""
            if not name:
                continue
            topic = f"TODAY: {name}"
            forecast = event.get("forecast") or ""
            previous = event.get("previous") or ""
            why_now  = f"Scheduled today ({today_str})."
            if forecast:
                why_now += f" Est: {forecast}."
            if previous:
                why_now += f" Prev: {previous}."
            if not self._seen("econ_today", name[:40]):
                signals.append({
                    "brand":   "MFD",
                    "topic":   topic,
                    "hook":    f"HIGH-impact data today: {name}",
                    "score":   5,
                    "why_now": why_now,
                    "source":  "financial_datasets",
                })

        for event in (tomorrow_events if isinstance(tomorrow_events, list) else []):
            name = event.get("event") or event.get("title") or ""
            if not name:
                continue
            topic    = f"TOMORROW: {name}"
            forecast = event.get("forecast") or ""
            why_now  = f"Scheduled tomorrow ({tomorrow_str})."
            if forecast:
                why_now += f" Est: {forecast}."
            if not self._seen("econ_tomorrow", name[:40]):
                signals.append({
                    "brand":   "MFD",
                    "topic":   topic,
                    "hook":    f"Watch tomorrow: {name}",
                    "score":   5,
                    "why_now": why_now,
                    "source":  "financial_datasets",
                })

        return signals


# ── Low-level API helpers ─────────────────────────────────────────────────────

async def _fetch_price_snapshots(tickers: list[str]) -> dict[str, dict]:
    """Fetch price snapshots for all tickers in parallel."""
    semaphore = asyncio.Semaphore(6)

    async def _one(ticker: str) -> tuple[str, dict]:
        async with semaphore:
            try:
                url = f"{FD_BASE}/prices/snapshot?ticker={ticker}"
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(url, headers=_fd_headers())
                if r.status_code == 200:
                    data = r.json()
                    return ticker, data.get("snapshot") or {}
                log.debug("[fd] snapshot %s → %d", ticker, r.status_code)
            except Exception as exc:
                log.debug("[fd] snapshot %s error: %s", ticker, exc)
            return ticker, {}

    results = await asyncio.gather(*[_one(t) for t in tickers])
    return dict(results)


async def _fetch_insider_trades(
    ticker: str,
    filing_date_gte: str = "",
    limit: int = 20,
) -> list[dict]:
    try:
        params = f"?ticker={ticker}&limit={limit}"
        if filing_date_gte:
            params += f"&filing_date_gte={filing_date_gte}"
        url = f"{FD_BASE}/insider-trades{params}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=_fd_headers())
        if r.status_code == 200:
            return r.json().get("insider_trades") or []
        log.debug("[fd] insider-trades %s → %d", ticker, r.status_code)
    except Exception as exc:
        log.debug("[fd] insider-trades %s error: %s", ticker, exc)
    return []


async def _fetch_8k_filings(
    ticker: str,
    filing_date_gte: str = "",
) -> list[dict]:
    try:
        params = f"?ticker={ticker}&form_type=8-K&limit=10"
        if filing_date_gte:
            params += f"&filing_date_gte={filing_date_gte}"
        url = f"{FD_BASE}/filings{params}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=_fd_headers())
        if r.status_code == 200:
            return r.json().get("filings") or []
        log.debug("[fd] filings %s → %d", ticker, r.status_code)
    except Exception as exc:
        log.debug("[fd] filings %s error: %s", ticker, exc)
    return []


async def _fetch_earnings_feed(limit: int = 50) -> list[dict]:
    try:
        url = f"{FD_BASE}/earnings/?limit={limit}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=_fd_headers())
        if r.status_code == 200:
            return r.json().get("earnings") or []
        log.debug("[fd] earnings feed → %d", r.status_code)
    except Exception as exc:
        log.debug("[fd] earnings feed error: %s", exc)
    return []


async def get_price_snapshots() -> dict[str, dict]:
    """Public: fetch price snapshots for the standard watchlist."""
    return await _fetch_price_snapshots(_PRICE_TICKERS)


# ── Supabase write ────────────────────────────────────────────────────────────

async def _write_signal(sig: dict) -> None:
    """Write one signal dict to Supabase signals table."""
    import supabase_logger as _sb
    await _sb._signal(
        brand=sig.get("brand", "MFD"),
        topic=sig.get("topic", ""),
        hook=sig.get("hook", ""),
        score=sig.get("score", 5),
        why_now=sig.get("why_now", ""),
        source=sig.get("source", "financial_datasets"),
    )


# ── Utilities ─────────────────────────────────────────────────────────────────

def _safe_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _safe_call(coro) -> Any:
    try:
        return await coro
    except Exception as exc:
        log.debug("[fd] safe_call error: %s", exc)
        return None
