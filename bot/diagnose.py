r"""
diagnose.py -- Targeted Kalshi API debug for KXBTC15M volume issue.

Usage (Windows Terminal):
    cd C:\Users\andre\Desktop\kalshi-bot\bot
    python diagnose.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_client import KalshiClient


def _pp(obj) -> None:
    """Pretty-print JSON."""
    print(json.dumps(obj, indent=2, default=str))


async def main() -> None:
    client = KalshiClient()
    try:
        SEP = "=" * 80

        # ── 1. Fetch single known ticker directly ─────────────────────────────
        ticker = "KXBTC15M-26MAR210115-15"
        print(f"\n{SEP}")
        print(f"TEST 1: GET /markets/{ticker}")
        print(SEP)
        try:
            resp = await client._get(f"/markets/{ticker}")
            _pp(resp)
        except Exception as e:
            print(f"ERROR: {e}")

        # ── 2. Fetch by event_ticker series ───────────────────────────────────
        print(f"\n{SEP}")
        print("TEST 2: GET /markets?series_ticker=KXBTC15M&limit=5")
        print(SEP)
        try:
            resp = await client._get("/markets", params={"series_ticker": "KXBTC15M", "limit": 5, "status": "open"})
            _pp(resp)
        except Exception as e:
            print(f"ERROR: {e}")

        # ── 3. Try event_ticker param ──────────────────────────────────────────
        print(f"\n{SEP}")
        print("TEST 3: GET /markets?event_ticker=KXBTC15M&limit=5")
        print(SEP)
        try:
            resp = await client._get("/markets", params={"event_ticker": "KXBTC15M", "limit": 5, "status": "open"})
            _pp(resp)
        except Exception as e:
            print(f"ERROR: {e}")

        # ── 4. Try the /events endpoint ───────────────────────────────────────
        print(f"\n{SEP}")
        print("TEST 4: GET /events?series_ticker=KXBTC15M&limit=3")
        print(SEP)
        try:
            resp = await client._get("/events", params={"series_ticker": "KXBTC15M", "limit": 3, "status": "open"})
            _pp(resp)
        except Exception as e:
            print(f"ERROR: {e}")

        # ── 5. Single market orderbook ────────────────────────────────────────
        print(f"\n{SEP}")
        print(f"TEST 5: GET /markets/{ticker}/orderbook")
        print(SEP)
        try:
            resp = await client._get(f"/markets/{ticker}/orderbook", params={"depth": 5})
            _pp(resp)
        except Exception as e:
            print(f"ERROR: {e}")

        # ── 6. Raw /markets with no filters — inspect first KXBTC result ──────
        print(f"\n{SEP}")
        print("TEST 6: GET /markets?limit=200 — find first KXBTC ticker and dump it")
        print(SEP)
        try:
            resp     = await client._get("/markets", params={"limit": 200, "status": "open"})
            markets  = resp.get("markets", [])
            btc_hits = [m for m in markets if "KXBTC" in (m.get("ticker") or "").upper()]
            print(f"Found {len(btc_hits)} KXBTC markets in first 200 results")
            if btc_hits:
                print("\nFirst KXBTC market — full raw object:")
                _pp(btc_hits[0])
                print(f"\nAll KXBTC tickers in this page:")
                for m in btc_hits:
                    vol_fp = m.get("volume_fp")
                    vol    = float(vol_fp) / 100 if vol_fp is not None else "null(volume_fp missing)"
                    print(f"  {m.get('ticker'):<45}  volume_fp={vol_fp!r:>12}  => ${vol}")
            else:
                print("No KXBTC markets in first 200. Checking all volume_fp values in page:")
                for m in markets[:5]:
                    print(f"  ticker={m.get('ticker')}  volume_fp={m.get('volume_fp')!r}")
        except Exception as e:
            print(f"ERROR: {e}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
