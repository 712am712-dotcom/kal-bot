r"""
diagnose.py -- Targeted Kalshi market diagnostic with rate-limit-safe delays.

Probes known series directly instead of paginating all 50k markets.
The /markets pagination only surfaces sports parlays — political/economic
markets need direct series_ticker queries.

Usage:
    cd C:\Users\andre\Desktop\kalshi-bot\bot
    python diagnose.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kalshi_client import KalshiClient
from main import extract_market_fields


def _vol(m: dict) -> float:
    fp = m.get("volume_fp")
    return float(fp) if fp is not None else 0.0


def _yes(m: dict) -> float:
    v = m.get("yes_ask_dollars") or m.get("yes_bid_dollars")
    return float(v) if v is not None else 0.5


async def probe(client: KalshiClient, series: str, limit: int = 10) -> list[dict]:
    """Fetch open markets for a series, return empty list on any error."""
    await asyncio.sleep(0.5)   # respect rate limit
    try:
        resp = await client._get("/markets", params={
            "series_ticker": series, "status": "open", "limit": limit
        })
        return resp.get("markets", [])
    except Exception as e:
        err = str(e)
        if "429" in err:
            await asyncio.sleep(3.0)
            try:
                resp = await client._get("/markets", params={
                    "series_ticker": series, "status": "open", "limit": limit
                })
                return resp.get("markets", [])
            except Exception:
                pass
        return []


async def main() -> None:
    client = KalshiClient()
    SEP = "=" * 100
    all_found: list[dict] = []

    try:
        # ── SECTION 1: Crypto markets ──────────────────────────────────────────
        print(f"\n{SEP}")
        print("SECTION 1: Crypto markets — all known series")
        print(SEP)

        CRYPTO_SERIES = [
            ("KXBTC15M",  "BTC 15min directional"),
            ("KXBTCH",    "BTC 1hr (does this exist?)"),
            ("KXBTC1H",   "BTC 1hr alt name"),
            ("KXBTC4H",   "BTC 4hr"),
            ("KXBTCW",    "BTC weekly"),
            ("KXBTCD",    "BTC daily price level"),
            ("KXBTC",     "BTC today close range"),
            ("KXETH15M",  "ETH 15min"),
            ("KXETHD",    "ETH daily"),
            ("KXSOL15M",  "SOL 15min"),
            ("KXSOLD",    "SOL daily"),
        ]

        print(f"\n{'SERIES':<14} {'DESCRIPTION':<30} {'OPEN':>5}  {'BEST VOLUME':>14}  TOP TICKER")
        print("-" * 100)
        for series, desc in CRYPTO_SERIES:
            markets = await probe(client, series, limit=5)
            if markets:
                best = max(markets, key=_vol)
                v = _vol(best)
                all_found.extend(markets)
                print(f"{series:<14} {desc:<30} {len(markets):>5}  ${v:>12,.2f}  {best.get('ticker','?')}")
                if v > 0:
                    fields = extract_market_fields(best)
                    print(f"  {'':14} {'volume_fp raw:':30} {str(best.get('volume_fp')):>15}  Kal reads: ${fields['volume']:,.2f}  {'OK' if abs(v - fields['volume']) < 0.01 else 'BUG'}")
            else:
                print(f"{series:<14} {desc:<30} {0:>5}  (no open markets)")

        # ── SECTION 2: Political / policy markets ─────────────────────────────
        print(f"\n{SEP}")
        print("SECTION 2: Political / policy markets")
        print(SEP)

        POLITICAL_SERIES = [
            ("TRUMPAPPROVE",   "Trump approval rating"),
            ("KXTRUMP",        "Trump actions/decisions"),
            ("PRES2028",       "2028 presidential race"),
            ("KXCONGRESS",     "Congress / legislation"),
            ("KXPOLITICS",     "Politics (generic)"),
            ("USPREZWIN",      "US presidential win"),
            ("KXELECTION",     "Election markets"),
            ("KXSENATE",       "Senate seats"),
            ("KXHOUSE",        "House seats"),
            ("KXTARIFF",       "Tariffs / trade policy"),
        ]

        print(f"\n{'SERIES':<18} {'DESCRIPTION':<32} {'OPEN':>5}  {'BEST VOLUME':>14}  TOP TICKER")
        print("-" * 100)
        for series, desc in POLITICAL_SERIES:
            markets = await probe(client, series, limit=10)
            if markets:
                best = max(markets, key=_vol)
                v = _vol(best)
                all_found.extend(markets)
                print(f"{series:<18} {desc:<32} {len(markets):>5}  ${v:>12,.2f}  {best.get('ticker','?')[:40]}")
            else:
                print(f"{series:<18} {desc:<32} {0:>5}  (no open markets)")

        # ── SECTION 3: Macro / economic markets ───────────────────────────────
        print(f"\n{SEP}")
        print("SECTION 3: Macro / economic / financial markets")
        print(SEP)

        MACRO_SERIES = [
            ("KXFED",          "Fed funds rate"),
            ("FEDFUNDS",       "Fed funds alt name"),
            ("KXRATE",         "Interest rates"),
            ("KXCPI",          "CPI inflation"),
            ("KXPCE",          "PCE / core inflation"),
            ("KXGDP",          "GDP growth"),
            ("KXUNEMP",        "Unemployment rate"),
            ("KXJOBS",         "Nonfarm payrolls"),
            ("KXGOLD",         "Gold price"),
            ("KXOIL",          "Oil / WTI"),
            ("KXDXY",          "US Dollar index"),
            ("KXVIX",          "VIX volatility"),
            ("INXD",           "S&P 500 daily"),
            ("INXU",           "S&P 500 up/down"),
            ("KXINX",          "S&P 500 variants"),
            ("KXNAS",          "Nasdaq"),
            ("KXSPX",          "SPX variants"),
            ("KXEARNINGS",     "Earnings results"),
            ("KXRECESSION",    "Recession probability"),
            ("KXDEBT",         "Debt ceiling / deficit"),
        ]

        print(f"\n{'SERIES':<18} {'DESCRIPTION':<32} {'OPEN':>5}  {'BEST VOLUME':>14}  TOP TICKER")
        print("-" * 100)
        for series, desc in MACRO_SERIES:
            markets = await probe(client, series, limit=10)
            if markets:
                best = max(markets, key=_vol)
                v = _vol(best)
                all_found.extend(markets)
                print(f"{series:<18} {desc:<32} {len(markets):>5}  ${v:>12,.2f}  {best.get('ticker','?')[:40]}")
            else:
                print(f"{series:<18} {desc:<32} {0:>5}  (no open markets)")

        # ── SECTION 4: Sample from pagination to find what IS out there ────────
        print(f"\n{SEP}")
        print("SECTION 4: One page (200 markets) from API — show ALL non-zero volume and their categories")
        print(SEP)

        await asyncio.sleep(1.0)
        resp = await client._get("/markets", params={"status": "open", "limit": 200})
        page_markets = resp.get("markets", [])
        nonzero = [(m, _vol(m)) for m in page_markets if _vol(m) > 50]
        nonzero.sort(key=lambda x: x[1], reverse=True)

        print(f"\nMarkets with volume > $50 in first page ({len(page_markets)} total):")
        print(f"{'VOLUME':>12}  {'YES%':>6}  {'CAT':<20}  {'TICKER'}")
        print("-" * 80)
        for m, v in nonzero[:30]:
            cat = (m.get("category") or "none").strip() or "none"
            print(f"${v:>10,.2f}  {_yes(m):>5.1%}  {cat:<20}  {m.get('ticker','?')[:50]}")
        if not nonzero:
            print("  (none — first page is all $0 parlays)")

        # ── SECTION 5: Grand summary — all found markets with real volume ───────
        print(f"\n{SEP}")
        print("SECTION 5: GRAND SUMMARY — all markets found via targeted queries, volume > $100")
        print(SEP)

        seen: set[str] = set()
        unique: list[dict] = []
        for m in all_found:
            t = m.get("ticker", "")
            if t and t not in seen:
                seen.add(t)
                unique.append(m)

        tradeable = [m for m in unique if _vol(m) >= 100]
        tradeable.sort(key=_vol, reverse=True)

        if tradeable:
            print(f"\n{len(tradeable)} markets with volume >= $100:\n")
            print(f"{'#':<4} {'VOLUME':>12}  {'YES%':>6}  {'MINS LEFT':>10}  {'TICKER':<45}  TITLE")
            print("-" * 110)
            import datetime as _dt
            now = _dt.datetime.now(_dt.timezone.utc)
            for i, m in enumerate(tradeable, 1):
                t    = (m.get("ticker") or "?")[:44]
                v    = _vol(m)
                yp   = _yes(m)
                ct   = m.get("close_time", "")
                mins = "?"
                if ct:
                    try:
                        close_dt = _dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        mins = f"{(close_dt - now).total_seconds() / 60:.0f}"
                    except Exception:
                        pass
                title = (m.get("title") or m.get("subtitle") or "?")[:35]
                print(f"{i:<4} ${v:>10,.2f}  {yp:>5.1%}  {mins:>9}m  {t:<45}  {title}")
        else:
            print("\n  No markets with volume >= $100 found via targeted series queries.")
            print("  Real high-volume markets may use different series names.")
            print("  Check the Kalshi API docs or web UI for correct series tickers.")

        # ── SECTION 6: Volume bug check ────────────────────────────────────────
        print(f"\n{SEP}")
        print("SECTION 6: Volume bug check — Kal's computed volume vs raw volume_fp")
        print(SEP)

        check_markets = [m for m in unique if _vol(m) > 0][:8]
        print(f"\n{'TICKER':<45}  {'RAW volume_fp':>18}  {'Kal reads':>13}  STATUS")
        print("-" * 90)
        for m in check_markets:
            ticker   = (m.get("ticker") or "?")[:44]
            raw_fp   = str(m.get("volume_fp", "MISSING"))
            raw_val  = _vol(m)
            kal_val  = extract_market_fields(m)["volume"]
            status   = "OK - FIXED" if abs(raw_val - kal_val) < 0.01 else f"BUG: got ${kal_val:,.2f}"
            print(f"{ticker:<45}  {raw_fp:>18}  ${kal_val:>11,.2f}  {status}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
