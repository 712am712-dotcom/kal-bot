"""
kalshi_client.py — Kal's async HTTP client for the Kalshi REST API v2.

Authentication: RSA-SHA256 signed requests.
  - KALSHI-ACCESS-KEY:       API key ID (UUID)
  - KALSHI-ACCESS-TIMESTAMP: milliseconds since epoch (string)
  - KALSHI-ACCESS-SIGNATURE: base64(RSA-SHA256(private_key, timestamp + METHOD + path))

Docs: https://trading-api.readme.io/reference/getting-started
"""
import base64
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import KALSHI_BASE_URL, settings


def _load_private_key():
    import os
    # Railway / cloud: store full PEM content in KALSHI_PRIVATE_KEY env var.
    # Local: fall back to reading from the file path.
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if raw:
        # Most deployment dashboards can't store real newlines — allow \n literals
        pem_bytes = raw.replace("\\n", "\n").encode()
    else:
        pem_path = Path(settings.kalshi_private_key_path)
        pem_bytes = pem_path.read_bytes()
    return serialization.load_pem_private_key(pem_bytes, password=None)


class KalshiClient:
    """Thin async wrapper around the Kalshi Trade API v2."""

    def __init__(self) -> None:
        self._base = KALSHI_BASE_URL
        self._private_key = _load_private_key()
        self._session = httpx.AsyncClient(
            base_url=self._base,
            timeout=30.0,
        )

    # ── Auth ────────────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """
        Build Kalshi auth headers using RSA-SHA256 signing.
        Signature covers: timestamp_ms_string + METHOD + /path (no query string).
        """
        ts = str(int(time.time() * 1000))
        # Strip query string from path for signing
        path_no_query = path.split("?")[0]
        msg = (ts + method.upper() + path_no_query).encode()

        sig_bytes = self._private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(sig_bytes).decode()

        return {
            "KALSHI-ACCESS-KEY": settings.kalshi_api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        headers = self._auth_headers("GET", path)
        r = await self._session.get(path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        headers = self._auth_headers("POST", path)
        r = await self._session.post(path, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _delete(self, path: str) -> dict:
        headers = self._auth_headers("DELETE", path)
        r = await self._session.delete(path, headers=headers)
        r.raise_for_status()
        return r.json()

    # ── Markets ─────────────────────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 200,
        cursor: str | None = None,
        status: str = "open",
        series_ticker: str | None = None,
    ) -> dict:
        """
        GET /markets — returns paginated list of markets.
        Response: { markets: [...], cursor: str | null }
        """
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self._get("/markets", params=params)

    async def get_all_open_markets(self) -> list[dict]:
        """Paginate through all open markets and return as flat list."""
        markets: list[dict] = []
        cursor: str | None = None
        while True:
            resp = await self.get_markets(limit=200, cursor=cursor)
            page = resp.get("markets", [])
            markets.extend(page)
            cursor = resp.get("cursor")
            if not cursor or not page:
                break
        return markets

    async def get_crypto_markets(self, min_volume: float = 0) -> list[dict]:
        """
        Return only BTC, ETH, and SOL price prediction markets.

        Kalshi crypto price tickers follow patterns like:
          KXBTCD-...  (BTC daily close)
          KXBTC-...   (BTC intraday)
          KXETHD-...  (ETH daily close)
          KXETH-...   (ETH intraday)
          KXSOLD-...  (SOL daily close)
          KXSOL-...   (SOL intraday)

        We also match against the Kalshi category field (e.g. "Crypto") and
        title keywords to catch any naming variations.

        min_volume=0  → research mode: all crypto markets pass (including zero-volume)
        min_volume>0  → live mode: only markets with sufficient liquidity
        """
        # Ticker prefixes are the most reliable signal
        CRYPTO_TICKER_PREFIXES = ("KXBTC", "KXETH", "KXSOL")

        # Kalshi category values observed for crypto markets — checked case-insensitively
        CRYPTO_CATEGORIES = ("crypto", "cryptocurrency", "bitcoin", "ethereum", "solana")

        # Coin keywords in the title
        CRYPTO_TITLE_KEYWORDS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol")

        # Price-concept keywords — market must mention a price threshold
        PRICE_KEYWORDS = ("above", "below", "close", "price", "exceed", "higher", "lower",
                          "at least", "over", "under")

        all_markets = await self.get_all_open_markets()

        # ── Debug: show raw sample before any filtering ───────────────────────
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.info(
            "[kalshi] raw market sample (first 10 of %d):",
            len(all_markets),
        )
        for _m in all_markets[:10]:
            _log.info(
                "  ticker=%-30s  category=%-20s  yes_ask=%-6s  volume=%s",
                _m.get("ticker", "?"),
                _m.get("category", "?"),
                _m.get("yes_ask", "null"),
                _m.get("volume", "null"),
            )

        # ── Filter with per-stage counters ────────────────────────────────────
        n_total        = len(all_markets)
        n_placeholder  = 0   # 50/50 + zero volume — not yet opened
        n_not_crypto   = 0   # doesn't match any crypto signal
        n_low_volume   = 0   # below min_volume (live mode only)
        results: list[dict] = []

        for m in all_markets:
            ticker   = (m.get("ticker")   or "").upper()
            title    = (m.get("title")    or m.get("subtitle") or "").lower()
            category = (m.get("category") or "").lower()

            # Kalshi v2 field names:
            #   volume_fp       — dollar volume as decimal string (already dollars, NOT cents)
            #   yes_ask_dollars — YES ask price as a decimal fraction (0.0–1.0)
            # NOTE: despite the _fp suffix, volume_fp is already in dollars (e.g. "75716.31" = $75,716).
            # Do NOT divide by 100 — that was a bug that gave $757 instead of $75,716.
            volume_fp   = m.get("volume_fp")
            volume      = float(volume_fp) if volume_fp is not None else 0.0

            yes_ask_raw   = m.get("yes_ask_dollars")
            yes_ask_known = yes_ask_raw is not None
            yes_ask       = float(yes_ask_raw) if yes_ask_known else None

            # ── Placeholder filter ────────────────────────────────────────
            # Drop when yes_ask is exactly 0.50 AND zero volume —
            # canonical Kalshi "not yet opened" placeholder state.
            if yes_ask_known and yes_ask == 0.50 and volume == 0:
                n_placeholder += 1
                continue

            # ── Crypto signal matching ────────────────────────────────────
            ticker_match   = any(ticker.startswith(p) for p in CRYPTO_TICKER_PREFIXES)
            category_match = any(kw in category for kw in CRYPTO_CATEGORIES)
            title_has_coin  = any(kw in title for kw in CRYPTO_TITLE_KEYWORDS)
            title_has_price = any(kw in title for kw in PRICE_KEYWORDS)
            title_match     = title_has_coin and title_has_price

            if not (ticker_match or category_match or title_match):
                n_not_crypto += 1
                continue

            # ── Volume filter (research=0 passes all; live applies threshold) ─
            if volume < min_volume:
                n_low_volume += 1
                continue

            results.append(m)

        import structlog as _sl
        _slog = _sl.get_logger(__name__)
        _slog.info(
            "crypto_filter_complete",
            total_markets=n_total,
            dropped_placeholder=n_placeholder,
            dropped_not_crypto=n_not_crypto,
            dropped_low_volume=n_low_volume,
            passing=len(results),
            min_volume=min_volume,
        )

        return results

    async def get_top_event_markets(
        self,
        limit: int = 30,
        min_volume: float = 0.0,
        pages: int = 5,
    ) -> list[dict]:
        """
        Return the top non-crypto event markets sorted by volume descending.

        Fetches `pages` pages (200 each) from the open-markets endpoint and picks
        the highest-volume non-crypto results. The Kalshi API doesn't expose a
        sort-by-volume parameter, so we paginate a sample and sort locally.

        pages=5 → 1000 markets, ~2–3 API calls (with cursor pagination).
        This is enough to reliably surface high-volume political / economic markets.
        """
        CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXMVE")  # KXMVE = parlay/multi

        all_markets: list[dict] = []
        cursor: str | None = None
        for _ in range(pages):
            resp = await self.get_markets(limit=200, cursor=cursor, status="open")
            page = resp.get("markets", [])
            all_markets.extend(page)
            cursor = resp.get("cursor")
            if not cursor or not page:
                break

        # Strip crypto and parlay markets
        event: list[dict] = []
        for m in all_markets:
            ticker = (m.get("ticker") or "").upper()
            if not any(ticker.startswith(p) for p in CRYPTO_PREFIXES):
                event.append(m)

        # Sort by volume descending
        def _vol(m: dict) -> float:
            fp = m.get("volume_fp")
            return float(fp) if fp is not None else 0.0

        event.sort(key=_vol, reverse=True)

        import structlog as _sl
        _sl.get_logger(__name__).info(
            "event_markets_sampled",
            pages_fetched=min(pages, len(all_markets) // 200 + 1),
            total_sampled=len(all_markets),
            non_crypto=len(event),
            returning=min(limit, len([e for e in event if _vol(e) >= min_volume])),
            min_volume=min_volume,
        )

        return [m for m in event[:limit] if _vol(m) >= min_volume]

    async def get_15min_markets(self) -> list[dict]:
        """
        Fetch the currently active 15-minute price markets for BTC, ETH, and SOL.

        Uses targeted series_ticker queries instead of scanning all 50k markets —
        3 small API calls instead of 250+ paginated pages.

        For each coin, returns the single soonest-closing open market that still
        has at least 1 minute of life left (enough time to analyze and fill).
        Markets under 1 minute are skipped — too late to trade safely.

        Returns a list of 0–3 market dicts.
        """
        import datetime

        SERIES = [
            ("KXBTC15M", "BTC"),
            ("KXETH15M", "ETH"),
            ("KXSOL15M", "SOL"),
        ]

        results: list[dict] = []
        now = datetime.datetime.now(datetime.timezone.utc)

        for series_ticker, coin in SERIES:
            try:
                resp = await self._get(
                    "/markets",
                    params={"series_ticker": series_ticker, "status": "open", "limit": 100},
                )
                markets = resp.get("markets", [])
                if not markets:
                    import logging as _l
                    _l.getLogger(__name__).debug(
                        "[kalshi] no open markets for series %s", series_ticker
                    )
                    continue

                # Pick the soonest-closing market with >= 1 minute remaining
                candidates: list[tuple[float, dict]] = []
                for m in markets:
                    ct_str = (m.get("close_time") or "").strip()
                    if not ct_str:
                        continue
                    try:
                        ct      = datetime.datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        mins    = (ct - now).total_seconds() / 60
                        if mins >= 1.0:
                            candidates.append((mins, m))
                    except ValueError:
                        continue

                if candidates:
                    candidates.sort(key=lambda x: x[0])   # soonest first
                    mins_left, chosen = candidates[0]
                    import logging as _l
                    _l.getLogger(__name__).info(
                        "[kalshi] %s next market: %s  (%.1f min left)",
                        coin, chosen.get("ticker"), mins_left,
                    )
                    results.append(chosen)

            except Exception as exc:
                import logging as _l
                _l.getLogger(__name__).warning(
                    "[kalshi] get_15min_markets failed for %s: %s", series_ticker, exc
                )

        return results

    async def get_market(self, ticker: str) -> dict:
        """GET /markets/{ticker} — single market detail."""
        resp = await self._get(f"/markets/{ticker}")
        return resp.get("market", resp)

    async def get_market_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """GET /markets/{ticker}/orderbook — best bids/asks."""
        return await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ── Portfolio ────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict:
        """GET /portfolio/balance — cash balance."""
        return await self._get("/portfolio/balance")

    async def get_positions(self) -> list[dict]:
        """GET /portfolio/positions — all open positions."""
        resp = await self._get("/portfolio/positions")
        return resp.get("market_positions", [])

    async def get_fills(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        """GET /portfolio/fills — trade fills."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        resp = await self._get("/portfolio/fills", params=params)
        return resp.get("fills", [])

    # ── Orders ───────────────────────────────────────────────────────────────

    async def place_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        contracts: int,
        price_cents: int,   # 1–99 cents
        order_type: str = "limit",
    ) -> dict:
        """
        POST /portfolio/orders — place a limit order.
        In demo_mode, returns a fake response without hitting the API.
        """
        if settings.demo_mode:
            return {
                "order": {
                    "id": f"demo-{ticker}-{int(time.time())}",
                    "ticker": ticker,
                    "side": side,
                    "contracts": contracts,
                    "price": price_cents,
                    "type": order_type,
                    "status": "resting",
                    "demo": True,
                }
            }

        body = {
            "action": "buy",
            "buy_max_cost": contracts * price_cents,
            "client_order_id": f"bot-{ticker}-{int(time.time())}",
            "count": contracts,
            "side": side,
            "ticker": ticker,
            "type": order_type,
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents

        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        """DELETE /portfolio/orders/{order_id}."""
        if settings.demo_mode:
            return {"order": {"id": order_id, "status": "cancelled", "demo": True}}
        return await self._delete(f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """GET /portfolio/orders — list orders."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        resp = await self._get("/portfolio/orders", params=params)
        return resp.get("orders", [])

    # ── Diagnostics ──────────────────────────────────────────────────────────

    async def diagnose_crypto_markets(self, top_n: int = 20) -> None:
        """
        Fetch every open market, find anything that looks remotely crypto-related,
        sort by volume descending, and print the top N in a readable table.

        Intentionally uses the loosest possible matching so nothing is hidden.
        Call this before running any Claude analysis to confirm real liquidity.
        """
        all_markets = await self.get_all_open_markets()

        COIN_SIGNALS = ("btc", "eth", "sol", "bitcoin", "ethereum", "solana",
                        "kxbtc", "kxeth", "kxsol", "crypto")

        crypto: list[dict] = []
        for m in all_markets:
            ticker   = (m.get("ticker")   or "").lower()
            title    = (m.get("title")    or m.get("subtitle") or "").lower()
            category = (m.get("category") or "").lower()
            combined = f"{ticker} {title} {category}"
            if any(sig in combined for sig in COIN_SIGNALS):
                crypto.append(m)

        # Sort by volume descending (None / missing treated as 0)
        crypto.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)

        print(f"\n{'='*100}")
        print(f"KALSHI CRYPTO MARKET DIAGNOSTIC — {len(crypto)} markets found out of {len(all_markets)} total open")
        print(f"{'='*100}")
        print(f"{'#':<4} {'TICKER':<35} {'VOLUME':>10} {'YES_ASK':>8} {'CAT':<18} TITLE")
        print(f"{'-'*100}")

        def _vol(m: dict) -> float:
            fp = m.get("volume_fp")
            return float(fp) if fp is not None else 0.0

        def _yes(m: dict) -> str:
            v = m.get("yes_ask_dollars")
            return f"{float(v):.0%}" if v is not None else "null"

        for i, m in enumerate(crypto[:top_n], start=1):
            ticker   = (m.get("ticker")   or "?")[:34]
            title    = (m.get("title")    or m.get("subtitle") or "?")[:55]
            volume   = _vol(m)
            category = (m.get("category") or "?")[:17]
            print(f"{i:<4} {ticker:<35} ${volume:>9,.0f} {_yes(m):>8} {category:<18} {title}")

        if len(crypto) > top_n:
            remaining = crypto[top_n:]
            with_vol  = [m for m in remaining if _vol(m) > 0]
            print(f"\n  ... and {len(remaining)} more ({len(with_vol)} with volume > $0)")

        # Volume summary
        with_volume = [m for m in crypto if _vol(m) > 0]
        with_500    = [m for m in crypto if _vol(m) >= 500]
        zero_vol    = [m for m in crypto if _vol(m) == 0]
        fifty_fifty = [m for m in crypto
                       if m.get("yes_ask_dollars") == 0.50 and _vol(m) == 0]
        print(f"\n{'─'*100}")
        print(f"  Total crypto markets:        {len(crypto)}")
        print(f"  With any volume (> $0):      {len(with_volume)}")
        print(f"  With real liquidity (≥$500): {len(with_500)}")
        print(f"  Zero volume:                 {len(zero_vol)}")
        print(f"  50/50 zero-vol placeholders: {len(fifty_fifty)}")
        print(f"{'='*100}\n")

    # ── Cleanup ──────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._session.aclose()
