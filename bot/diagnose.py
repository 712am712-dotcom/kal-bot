r"""
diagnose.py -- Comprehensive Kalshi API diagnostic.

Shows:
  1. Every open BTC market (all series) with ticker, yes_price, volume, close_time
  2. Full raw API response for the current BTC 15min market
  3. ALL volume-related fields and their values (finds the real volume field)
  4. Top 10 markets by volume across ALL categories

Usage:
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
    print(json.dumps(obj, indent=2, default=str))


def _all_volume_fields(m: dict) -> dict:
    """Extract every field name that looks remotely volume-related."""
    vol_keys = {}
    for k, v in m.items():
        kl = k.lower()
        if any(w in kl for w in ("vol", "liquid", "interest", "notional", "size", "amount", "traded", "contract")):
            vol_keys[k] = v
    return vol_keys


def _kal_computed_volume(m: dict) -> float:
    """What Kal's extract_market_fields() currently computes for volume."""
    volume_fp = m.get("volume_fp")
    return float(volume_fp) / 100.0 if volume_fp is not None else 0.0


def _yes_price(m: dict) -> float:
    yes_ask = m.get("yes_ask_dollars")
    yes_bid = m.get("yes_bid_dollars")
    if yes_ask is not None:
        return float(yes_ask)
    if yes_bid is not None:
        return float(yes_bid)
    return 0.5


async def main() -> None:
    client = KalshiClient()
    SEP = "=" * 90

    try:
        # ── SECTION 1: All open BTC markets ────────────────────────────────────
        print(f"\n{SEP}")
        print("SECTION 1: All open BTC markets (series_ticker=KXBTC15M, KXBTCH, KXBTCD)")
        print(SEP)

        btc_series = ["KXBTC15M", "KXBTCH", "KXBTCD", "KXBTC"]
        all_btc = []
        for series in btc_series:
            try:
                resp = await client._get(
                    "/markets",
                    params={"series_ticker": series, "status": "open", "limit": 20},
                )
                markets = resp.get("markets", [])
                if markets:
                    print(f"\n  Series {series}: {len(markets)} open markets")
                    all_btc.extend(markets)
                else:
                    print(f"\n  Series {series}: 0 open markets")
            except Exception as e:
                print(f"\n  Series {series}: ERROR — {e}")

        if all_btc:
            print(f"\n{'#':<4} {'TICKER':<45} {'YES_PRICE':>10} {'KAL_VOLUME':>12} {'CLOSE_TIME'}")
            print("-" * 90)
            for i, m in enumerate(all_btc, 1):
                ticker     = m.get("ticker", "?")
                yes_p      = _yes_price(m)
                kal_vol    = _kal_computed_volume(m)
                close_time = m.get("close_time", "?")
                print(f"{i:<4} {ticker:<45} {yes_p:>10.1%} ${kal_vol:>10,.2f}  {close_time}")

        # ── SECTION 2: Full raw API response for first BTC 15min market ────────
        print(f"\n{SEP}")
        print("SECTION 2: Full raw API response for the first open KXBTC15M market")
        print(SEP)

        try:
            resp = await client._get(
                "/markets",
                params={"series_ticker": "KXBTC15M", "status": "open", "limit": 1},
            )
            markets_15m = resp.get("markets", [])
            if markets_15m:
                first = markets_15m[0]
                print(f"\nRaw market object for: {first.get('ticker')}")
                _pp(first)

                # ── SECTION 3: Volume field comparison ───────────────────────────
                print(f"\n{SEP}")
                print("SECTION 3: Volume field analysis — what does Kal read vs reality?")
                print(SEP)

                print("\nAll volume-related fields found in raw API response:")
                vol_fields = _all_volume_fields(first)
                if vol_fields:
                    for k, v in vol_fields.items():
                        print(f"  {k:<35} = {v!r}")
                else:
                    print("  (none found)")

                print(f"\nKal currently reads:  volume_fp = {first.get('volume_fp')!r}")
                print(f"Kal computed volume:  ${_kal_computed_volume(first):,.2f}")

                # Also check single-market endpoint — sometimes more fields
                ticker = first.get("ticker", "")
                if ticker:
                    print(f"\nFetching single-market endpoint: GET /markets/{ticker}")
                    try:
                        single = await client._get(f"/markets/{ticker}")
                        single_market = single.get("market", single)
                        print(f"\nSingle-market volume fields:")
                        vol_fields_single = _all_volume_fields(single_market)
                        for k, v in vol_fields_single.items():
                            print(f"  {k:<35} = {v!r}")
                        print(f"\nFull single-market response:")
                        _pp(single_market)
                    except Exception as e:
                        print(f"ERROR fetching single market: {e}")
            else:
                print("No open KXBTC15M markets found.")
        except Exception as e:
            print(f"ERROR: {e}")

        # ── SECTION 4: Top 10 markets by volume across ALL categories ──────────
        print(f"\n{SEP}")
        print("SECTION 4: Top 10 markets by volume across ALL categories")
        print("  (tests multiple volume field names to find which one has real data)")
        print(SEP)

        try:
            resp = await client._get("/markets", params={"status": "open", "limit": 200})
            all_markets = resp.get("markets", [])
            print(f"\nFirst page: {len(all_markets)} markets")

            # Try every possible volume field
            candidate_fields = ["volume_fp", "volume", "dollar_volume", "open_interest_fp",
                                 "open_interest", "liquidity", "notional_value", "total_traded"]

            print("\nVolume field presence across first 200 markets:")
            for field in candidate_fields:
                present = sum(1 for m in all_markets if m.get(field) is not None and m.get(field) != 0)
                print(f"  {field:<35}: non-zero in {present:>4}/{len(all_markets)} markets")

            # Find best volume field (most non-zero values)
            best_field = max(
                candidate_fields,
                key=lambda f: sum(1 for m in all_markets if m.get(f) is not None and m.get(f) != 0),
            )
            print(f"\nBest volume field: {best_field!r}")

            # Sort by best field and show top 10
            def _vol_by_field(m: dict, field: str) -> float:
                v = m.get(field)
                if v is None:
                    return 0.0
                v = float(v)
                # volume_fp and open_interest_fp are fixed-point (divide by 100)
                if "_fp" in field:
                    v /= 100.0
                return v

            all_markets_sorted = sorted(
                all_markets,
                key=lambda m: _vol_by_field(m, best_field),
                reverse=True,
            )

            print(f"\nTop 10 markets by {best_field}:")
            print(f"{'#':<4} {'TICKER':<40} {'VOLUME':>12}  {'CATEGORY':<20} TITLE")
            print("-" * 100)
            for i, m in enumerate(all_markets_sorted[:10], 1):
                ticker   = (m.get("ticker") or "?")[:39]
                vol      = _vol_by_field(m, best_field)
                category = (m.get("category") or "?")[:19]
                title    = (m.get("title") or m.get("subtitle") or "?")[:40]
                print(f"{i:<4} {ticker:<40} ${vol:>10,.0f}  {category:<20} {title}")

            # Also show Kal's current computed volume for comparison
            print(f"\nFor comparison — Kal's current volume (volume_fp / 100):")
            all_markets_sorted_kal = sorted(
                all_markets,
                key=lambda m: _kal_computed_volume(m),
                reverse=True,
            )
            print(f"{'#':<4} {'TICKER':<40} {'KAL_VOLUME':>12}  {'REAL_VOL':>12}")
            print("-" * 75)
            for i, m in enumerate(all_markets_sorted_kal[:10], 1):
                ticker  = (m.get("ticker") or "?")[:39]
                kal_vol = _kal_computed_volume(m)
                real_vol = _vol_by_field(m, best_field)
                print(f"{i:<4} {ticker:<40} ${kal_vol:>10,.0f}  ${real_vol:>10,.0f}")

        except Exception as e:
            print(f"ERROR fetching all markets: {e}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
