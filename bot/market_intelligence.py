"""
market_intelligence.py — Kal's Market Intelligence Scanner.

Monitors the current BTC/ETH/SOL 15-minute Kalshi markets every N minutes
and fires Discord alerts to #intelligence for notable changes.

Zero Claude/Anthropic calls — pure Kalshi market data only.

Alert types:
  PRICE_MOVE      — yes_price shifted >10 percentage points since last scan
  VOLUME_SPIKE    — volume doubled since last scan (min $200 base)
  NEW_MARKET      — new window ticker appeared with volume already >$500
  HIGH_CONVICTION — market pricing at <20% or >80% with volume >$500
  HOURLY_SUMMARY  — always fires once per hour with full snapshot

Usage:
    scanner = IntelligenceScanner(kalshi_client)
    await scanner.scan()   # called from background task in main.py
"""
from __future__ import annotations

import datetime

import structlog

import discord_notifier as discord

log = structlog.get_logger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
PRICE_MOVE_THRESHOLD    = 0.10   # 10 percentage points
VOLUME_SPIKE_MULTIPLIER = 2.0    # volume at least doubled
NEW_MARKET_VOL_FLOOR    = 500.0  # $500 to count as noteworthy new market
HIGH_CONV_PRICE_LOW     = 0.08   # <8% = crowd says extremely unlikely
HIGH_CONV_PRICE_HIGH    = 0.92   # >92% = crowd says extremely likely
HIGH_CONV_VOL_FLOOR     = 500.0  # need real liquidity to call it conviction
HIGH_CONV_MIN_MINUTES   = 5.0    # must have >5 minutes left before close

SNAPSHOT_MIN_INTERVAL_SECS = 55 * 60  # minimum 55 minutes between snapshots


class IntelligenceScanner:
    """
    Stateful scanner that compares each scan's market snapshot against
    the previous one and fires targeted alerts.

    Deduplication: any (alert_type, ticker) pair fires at most once per
    market window (i.e., until that ticker disappears from the live market list).
    This prevents multi-fire on restarts or rapid scans within the same window.

    Snapshot: posted at most once per 55 minutes to prevent duplicates on restart.
    """

    def __init__(self, kalshi) -> None:
        self._kalshi = kalshi
        # {ticker: snap_dict} — snapshot from previous scan
        self._prev_snapshot: dict[str, dict] = {}
        # dedup keys: "ALERT_TYPE:ticker" — entries cleared when ticker disappears
        self._alerted_this_window: set[str] = set()
        # timestamp of last snapshot — 55-minute minimum gap
        self._last_snapshot_time: datetime.datetime | None = None
        # live prices set externally: {coin: price}  e.g. {"BTC": 68259.0}
        self.live_prices: dict[str, float] = {}

    # ── Main entry point ──────────────────────────────────────────────────────

    async def scan(self) -> int:
        """
        Run one intelligence scan. Returns number of alerts fired.
        Only calls get_15min_markets() (3 Kalshi API calls, 0 Claude calls).
        """
        try:
            markets = await self._kalshi.get_15min_markets()
        except Exception as exc:
            log.warning("intelligence_scan_kalshi_error", error=str(exc))
            return 0

        if not markets:
            log.debug("intelligence_scan_no_markets")
            return 0

        # Build current snapshot
        current: dict[str, dict] = {}
        for m in markets:
            ticker    = m.get("ticker", "")
            yes_ask   = m.get("yes_ask_dollars")
            yes_bid   = m.get("yes_bid_dollars")
            if yes_ask is not None:
                yes_price = float(yes_ask)
            elif yes_bid is not None:
                yes_price = float(yes_bid)
            else:
                yes_price = 0.50
            yes_price = max(0.01, min(0.99, yes_price))

            vol_fp = m.get("volume_fp")
            volume = float(vol_fp) / 100.0 if vol_fp is not None else 0.0

            coin  = next((c for c in ("BTC", "ETH", "SOL") if c in ticker.upper()), "")
            title = m.get("title") or m.get("subtitle") or ticker

            current[ticker] = {
                "ticker":     ticker,
                "coin":       coin,
                "title":      title,
                "yes_price":  yes_price,
                "volume":     volume,
                "close_time": m.get("close_time", ""),
            }

        # ── Window-based dedup: clear entries for tickers that have closed ────
        closed_tickers = set(self._prev_snapshot) - set(current)
        if closed_tickers:
            self._alerted_this_window = {
                k for k in self._alerted_this_window
                if not any(k.endswith(f":{t}") for t in closed_tickers)
            }

        alerts_fired = 0

        # ── Compare against previous snapshot ─────────────────────────────────
        for ticker, snap in current.items():
            prev = self._prev_snapshot.get(ticker)

            if prev is None:
                # New ticker (new window opened)
                if snap["volume"] >= NEW_MARKET_VOL_FLOOR:
                    if self._should_alert("NEW_MARKET", ticker):
                        await self._safe(discord.notify_intelligence_new_market(snap))
                        alerts_fired += 1
            else:
                # Price movement check
                price_shift = abs(snap["yes_price"] - prev["yes_price"])
                if price_shift >= PRICE_MOVE_THRESHOLD:
                    if self._should_alert("PRICE_MOVE", ticker):
                        await self._safe(
                            discord.notify_intelligence_price_move(snap, prev_price=prev["yes_price"])
                        )
                        alerts_fired += 1

                # Volume spike check — base must be ≥$200 so noise doesn't trigger
                if (prev["volume"] >= 200.0
                        and snap["volume"] >= prev["volume"] * VOLUME_SPIKE_MULTIPLIER):
                    if self._should_alert("VOLUME_SPIKE", ticker):
                        await self._safe(
                            discord.notify_intelligence_volume_spike(snap, prev_volume=prev["volume"])
                        )
                        alerts_fired += 1

            # Strong conviction — only extreme prices + volume + enough time left
            mins_left = _minutes_remaining(snap["close_time"])
            if (snap["volume"] >= HIGH_CONV_VOL_FLOOR
                    and mins_left >= HIGH_CONV_MIN_MINUTES
                    and (snap["yes_price"] <= HIGH_CONV_PRICE_LOW
                         or snap["yes_price"] >= HIGH_CONV_PRICE_HIGH)):
                if self._should_alert("HIGH_CONVICTION", ticker):
                    await self._safe(discord.notify_intelligence_high_conviction(snap))
                    alerts_fired += 1

        # ── Snapshot — at most once per 55 minutes ────────────────────────────
        now = datetime.datetime.utcnow()
        if (self._last_snapshot_time is None
                or (now - self._last_snapshot_time).total_seconds() >= SNAPSHOT_MIN_INTERVAL_SECS):
            self._last_snapshot_time = now
            await self._safe(
                discord.notify_intelligence_summary(list(current.values()), self.live_prices)
            )
            alerts_fired += 1

        # Advance snapshot
        self._prev_snapshot = current

        log.info(
            "intelligence_scan_complete",
            markets=len(current),
            alerts=alerts_fired,
        )
        return alerts_fired

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _should_alert(self, alert_type: str, ticker: str) -> bool:
        """Return True (and mark) if this (alert_type, ticker) hasn't fired this window."""
        key = f"{alert_type}:{ticker}"
        if key in self._alerted_this_window:
            return False
        self._alerted_this_window.add(key)
        return True

    @staticmethod
    async def _safe(coro) -> None:
        """Await a coroutine, swallowing exceptions so alerts never crash the scanner."""
        try:
            await coro
        except Exception as exc:
            log.warning("intelligence_alert_send_failed", error=str(exc))


def _minutes_remaining(close_time: str) -> float:
    """Return minutes until close_time. Returns 999.0 on parse error."""
    try:
        ct      = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (ct - now_utc).total_seconds() / 60.0)
    except Exception:
        return 999.0
