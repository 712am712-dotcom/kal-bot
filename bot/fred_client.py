"""
fred_client.py — FRED (Federal Reserve Economic Data) API client.

Pulls macro economic series from the St. Louis Fed's free REST API.
No rate limits on free tier. Series update on their own schedules.

Series tracked:
  DFF             — Fed Funds Rate (effective, daily)
  DGS2            — 2-Year Treasury yield (daily)
  DGS10           — 10-Year Treasury yield (daily)
  DGS30           — 30-Year Treasury yield (daily)
  T10Y2Y          — 10Y-2Y yield curve spread (daily, negative = inverted)
  CPIAUCSL        — CPI inflation (monthly)
  UNRATE          — Unemployment rate (monthly)
  DCOILWTICO      — WTI Crude Oil price (daily)
  GOLDPMGBD228NLBM — Gold price (daily, LBMA PM fix)
  MORTGAGE30US    — 30-year fixed mortgage rate (weekly)
  BAMLH0A0HYM2    — High yield corporate bond OAS (daily, credit fear gauge)

Bond market relationships Kal tracks:
  - Rising yields          → bonds fall → tech/growth stocks under pressure
  - Inverted curve (T10Y2Y < 0) → recession risk elevated (flag prominently)
  - Fed hikes              → dollar strengthens → gold + crypto weaken short-term
  - Wide HY spread         → credit stress → risk-off across all markets
  - Spiking mortgage rates → housing slows → watch homebuilder stocks

Usage:
    from fred_client import FredClient
    fred = FredClient(api_key="your_key")
    data = await fred.get_all()
    bond_block = fred.format_bond_block(data)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

# (series_id, key_name, unit, human_label, fred_units)
# fred_units: "lin" = raw level, "pc1" = year-over-year % change, "pch" = 1-period % change
# CPIAUCSL raw value is an index level (~314). Using "pc1" gives the YoY inflation rate (~2.8%).
FRED_SERIES: list[tuple[str, str, str, str, str]] = [
    ("DFF",              "fed_funds_rate", "%",  "Fed Funds Rate",        "lin"),
    ("DGS2",             "yield_2y",       "%",  "2-Year Treasury",       "lin"),
    ("DGS10",            "yield_10y",      "%",  "10-Year Treasury",      "lin"),
    ("DGS30",            "yield_30y",      "%",  "30-Year Treasury",      "lin"),
    ("T10Y2Y",           "yield_curve",    "%",  "10Y-2Y Yield Curve",    "lin"),
    ("CPIAUCSL",         "cpi",            "%",  "CPI YoY%",              "pc1"),  # pc1 = YoY% change — avoids the "327%" bug from raw index
    ("UNRATE",           "unemployment",   "%",  "Unemployment Rate",     "lin"),
    ("DCOILWTICO",       "oil_wti",        "$",  "WTI Crude Oil",         "lin"),
    ("GOLDPMGBD228NLBM", "gold",           "$",  "Gold (LBMA PM)",        "lin"),
    ("MORTGAGE30US",     "mortgage_30y",   "%",  "30-Year Mortgage Rate", "lin"),
    ("BAMLH0A0HYM2",     "hy_spread",      "%",  "High Yield Bond Spread","lin"),
]


class FredClient:
    """Async FRED API client. Fetches the most recent observation for each series."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def _get_series(self, series_id: str, units: str = "lin") -> float | None:
        """Fetch the most recent non-null value for a FRED series.

        units:
          "lin"  — raw level (default for rates, prices)
          "pc1"  — year-over-year percent change (use for CPI to get ~2.8%, not ~314)
          "pch"  — 1-period percent change
        """
        url = f"{FRED_BASE}/series/observations"
        params = {
            "series_id":        series_id,
            "api_key":          self._api_key,
            "file_type":        "json",
            "sort_order":       "desc",
            "limit":            10,        # last 10 — skip any '.' missing entries
            "observation_start": "2020-01-01",
            "units":            units,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            for obs in data.get("observations", []):
                val = obs.get("value", ".")
                if val not in (".", "", None):
                    return float(val)
        except Exception as exc:
            log.warning("[fred] %s fetch failed: %s", series_id, exc)
        return None

    async def get_all(self) -> dict[str, Any]:
        """
        Fetch all tracked FRED series concurrently. Returns a flat dict keyed
        by the series key names above. Missing values are None.

        Also adds:
          yield_curve_inverted (bool)  — True when T10Y2Y < 0
        """
        if not self._api_key:
            log.warning("[fred] FRED_API_KEY not set — returning empty data")
            return {}

        results: dict[str, Any] = {}
        coros = [(label, self._get_series(sid, fu)) for sid, label, _, _, fu in FRED_SERIES]
        fetched = await asyncio.gather(*[c for _, c in coros], return_exceptions=True)
        for (label, _), value in zip(coros, fetched):
            results[label] = value if not isinstance(value, Exception) else None

        # Derived signal
        curve = results.get("yield_curve")
        results["yield_curve_inverted"] = (curve is not None and curve < 0)

        log.info(
            "[fred] data_fetched fed_funds=%.2f yield_10y=%.2f curve=%.2f inverted=%s",
            results.get("fed_funds_rate") or 0,
            results.get("yield_10y") or 0,
            results.get("yield_curve") or 0,
            results["yield_curve_inverted"],
        )
        return results

    # ── Formatting helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _pct(v: float | None, decimals: int = 2) -> str:
        return f"{v:.{decimals}f}%" if v is not None else "N/A"

    @staticmethod
    def _dollar(v: float | None) -> str:
        if v is None:
            return "N/A"
        return f"${v:,.0f}"

    def format_bond_block(self, data: dict) -> str:
        """
        Format the BOND MARKET RIGHT NOW section for Discord posts.
        Plain text, one data point per line.
        """
        curve    = data.get("yield_curve")
        inverted = data.get("yield_curve_inverted", False)
        hy       = data.get("hy_spread")
        mort     = data.get("mortgage_30y")

        # Yield curve label
        curve_str = self._pct(curve)
        if inverted:
            curve_label = f"{curve_str} — INVERTED — recession signal"
        elif curve is not None and curve < 0.5:
            curve_label = f"{curve_str} — flattening, watch closely"
        else:
            curve_label = f"{curve_str} — NORMAL"

        # HY spread label
        if hy is not None:
            if hy > 6.0:
                spread_label = f"{self._pct(hy)} — wide = credit stress, risk off"
            elif hy > 4.0:
                spread_label = f"{self._pct(hy)} — elevated, caution"
            else:
                spread_label = f"{self._pct(hy)} — tight = risk on"
        else:
            spread_label = "N/A"

        # One-sentence bond market reading
        if inverted:
            reading = (
                "Bond market is pricing in recession risk — yield curve inversion is "
                "historically one of the most reliable leading indicators. Watch for "
                "flight-to-quality moves into Treasuries."
            )
        elif hy is not None and hy > 5.5:
            reading = (
                "Credit spreads are wide — institutions are paying a premium to avoid "
                "default risk. This is a risk-off signal. Expect pressure on equities and crypto."
            )
        elif hy is not None and hy < 3.5 and not inverted:
            reading = (
                "Bond market is saying risk is low — tight credit spreads and a normal "
                "yield curve. Consistent with a risk-on environment."
            )
        else:
            reading = (
                "Bond market conditions are neutral — no extreme signals from yields "
                "or credit spreads right now."
            )

        lines = [
            "**BOND MARKET RIGHT NOW:**",
            f"- Fed Funds Rate: {self._pct(data.get('fed_funds_rate'))}",
            (
                f"- 2Y yield: {self._pct(data.get('yield_2y'))} | "
                f"10Y yield: {self._pct(data.get('yield_10y'))} | "
                f"30Y yield: {self._pct(data.get('yield_30y'))}"
            ),
            f"- Yield curve (10Y-2Y): {curve_label}",
            f"- High yield spread: {spread_label}",
            f"- Mortgage rate: {self._pct(mort)}",
            f"- Bond market reading: {reading}",
        ]
        return "\n".join(lines)

    def format_macro_block(self, data: dict) -> str:
        """Format the KEY ECONOMIC DATA section."""
        from market_qa import check_price_sanity

        oil_wti = data.get("oil_wti")
        if oil_wti is not None:
            ok, _ = check_price_sanity("WTI Crude", oil_wti)
            if not ok:
                oil_wti = None

        gold = data.get("gold")
        if gold is not None:
            ok, _ = check_price_sanity("Gold", gold)
            if not ok:
                gold = None

        lines = [
            "**KEY ECONOMIC DATA:**",
            f"- CPI inflation (YoY): {self._pct(data.get('cpi'), decimals=1)}",
            f"- Unemployment: {self._pct(data.get('unemployment'), decimals=1)}",
            f"- WTI Crude: {self._dollar(oil_wti)}",
            f"- Gold: {self._dollar(gold)}",
        ]
        return "\n".join(lines)

    def market_open_bond_line(self, data: dict) -> str:
        """Compact one-line bond summary for the market open snapshot."""
        y10   = data.get("yield_10y")
        y2    = data.get("yield_2y")
        curve = data.get("yield_curve")
        inv   = data.get("yield_curve_inverted", False)

        y10_str   = self._pct(y10) if y10 is not None else "N/A"
        y2_str    = self._pct(y2)  if y2  is not None else "N/A"
        curve_str = self._pct(curve) if curve is not None else "N/A"
        label     = "inverted" if inv else "normal"

        return f"Bonds: 10Y yield {y10_str} | 2Y yield {y2_str} | Spread {curve_str} [{label}]"

    def build_rotational_context(self, data: dict) -> str:
        """
        Build a concise macro context string to inject into Claude prompts.
        Surfaces the most important signals Kal should factor into every trade.
        """
        if not data:
            return ""

        lines = ["--- BOND & MACRO CONTEXT ---"]

        ff   = data.get("fed_funds_rate")
        y10  = data.get("yield_10y")
        y2   = data.get("yield_2y")
        curv = data.get("yield_curve")
        hy   = data.get("hy_spread")
        oil  = data.get("oil_wti")
        gold = data.get("gold")
        inv  = data.get("yield_curve_inverted", False)

        if ff  is not None: lines.append(f"Fed Funds Rate: {self._pct(ff)}")
        if y10 is not None: lines.append(f"10Y Treasury: {self._pct(y10)}")
        if y2  is not None: lines.append(f"2Y Treasury: {self._pct(y2)}")
        if curv is not None:
            inv_str = " (INVERTED — recession signal)" if inv else ""
            lines.append(f"Yield curve (10Y-2Y): {self._pct(curv)}{inv_str}")
        if hy   is not None:
            stress = " (elevated — credit stress signal)" if hy > 4.5 else ""
            lines.append(f"HY bond spread: {self._pct(hy)}{stress}")
        if oil  is not None: lines.append(f"WTI Oil: {self._dollar(oil)}")
        if gold is not None: lines.append(f"Gold: {self._dollar(gold)}")

        return "\n".join(lines)
