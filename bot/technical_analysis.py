"""
technical_analysis.py — Binance candle data + pandas-ta indicators for Kal.

Fetches OHLCV candles from Binance (free, no API key required) and calculates
a full suite of technical indicators used to enrich Claude prompts and post
hourly TA summaries to Discord #analysis.

No Claude calls — all math, all local.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import httpx
import pandas as pd

log = logging.getLogger(__name__)

# Kraken public REST — no auth, no geo-block, free
# https://docs.kraken.com/api/docs/rest-api/get-ohlc-data
KRAKEN_BASE = "https://api.kraken.com/0/public"

SYMBOLS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
}

# Kraken intervals in minutes
INTERVALS = {
    "15m": 15,
    "1h":  60,
    "4h":  240,
    "1d":  1440,
}

COIN_FULL = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}


# ── Kraken fetch ──────────────────────────────────────────────────────────────

async def fetch_candles(symbol: str, interval_key: str, limit: int = 100) -> pd.DataFrame:
    """
    Fetch OHLCV candles from Kraken (free, no auth, no geo-block).
    interval_key: "15m" | "1h" | "4h" | "1d"
    Returns DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    interval_min = INTERVALS[interval_key]
    url = f"{KRAKEN_BASE}/OHLC"
    params = {"pair": symbol, "interval": interval_min}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")

    # result key is the pair name (may differ from input, e.g. XXBTZUSD)
    result = data.get("result", {})
    # Remove the "last" key (it's the timestamp of last candle, not OHLC)
    ohlc_key = next((k for k in result if k != "last"), None)
    if not ohlc_key:
        raise ValueError(f"No OHLC data in Kraken response for {symbol}")

    raw = result[ohlc_key]
    # Kraken candle: [time, open, high, low, close, vwap, volume, count]
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "vwap", "volume", "count"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    # Kraken returns up to 720 candles — take the last `limit`
    return df.tail(limit).reset_index(drop=True)


# ── Indicator calculation ──────────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """
    Run pandas-ta on a candle DataFrame. Returns a flat dict of indicator values
    (latest candle only — the values we pass to Claude and Discord).
    """
    try:
        import pandas_ta as ta  # type: ignore
    except ImportError:
        log.warning("[ta] pandas-ta not installed — returning empty indicators")
        return {}

    if len(df) < 30:
        log.warning("[ta] not enough candles (%d) for reliable indicators", len(df))
        return {}

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    out: dict[str, Any] = {}

    # RSI (14)
    try:
        rsi = ta.rsi(close, length=14)
        out["rsi"] = round(float(rsi.iloc[-1]), 1) if rsi is not None else None
    except Exception:
        out["rsi"] = None

    # MACD (12, 26, 9)
    try:
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None and len(macd_df.columns) >= 3:
            macd_col   = macd_df.columns[0]
            signal_col = macd_df.columns[2]
            hist_col   = macd_df.columns[1]
            out["macd"]        = round(float(macd_df[macd_col].iloc[-1]),   4)
            out["macd_signal"] = round(float(macd_df[signal_col].iloc[-1]), 4)
            out["macd_hist"]   = round(float(macd_df[hist_col].iloc[-1]),   4)
            # Crossover: check last two candles
            prev_hist = float(macd_df[hist_col].iloc[-2])
            curr_hist = out["macd_hist"]
            if prev_hist < 0 and curr_hist > 0:
                out["macd_cross"] = "bullish"
            elif prev_hist > 0 and curr_hist < 0:
                out["macd_cross"] = "bearish"
            else:
                out["macd_cross"] = "bullish" if curr_hist > 0 else "bearish"
    except Exception:
        out["macd"] = out["macd_signal"] = out["macd_hist"] = None
        out["macd_cross"] = None

    # Bollinger Bands (20, 2)
    try:
        bb = ta.bbands(close, length=20, std=2)
        if bb is not None:
            cols = list(bb.columns)
            # pandas-ta names: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
            lower  = next((c for c in cols if c.startswith("BBL")), None)
            mid    = next((c for c in cols if c.startswith("BBM")), None)
            upper  = next((c for c in cols if c.startswith("BBU")), None)
            if lower and mid and upper:
                out["bb_lower"]  = round(float(bb[lower].iloc[-1]),  2)
                out["bb_mid"]    = round(float(bb[mid].iloc[-1]),    2)
                out["bb_upper"]  = round(float(bb[upper].iloc[-1]),  2)
                curr_close = float(close.iloc[-1])
                bw = out["bb_upper"] - out["bb_lower"]
                pct = (curr_close - out["bb_lower"]) / bw if bw > 0 else 0.5
                out["bb_pct"] = round(pct, 3)
                if curr_close >= out["bb_upper"] * 0.995:
                    out["bb_position"] = "upper_band"
                elif curr_close <= out["bb_lower"] * 1.005:
                    out["bb_position"] = "lower_band"
                else:
                    out["bb_position"] = "middle"
    except Exception:
        out["bb_lower"] = out["bb_upper"] = out["bb_mid"] = None
        out["bb_position"] = None

    # EMAs
    for length in (9, 21, 50, 200):
        try:
            ema = ta.ema(close, length=length)
            out[f"ema{length}"] = round(float(ema.iloc[-1]), 2) if ema is not None else None
        except Exception:
            out[f"ema{length}"] = None

    # Volume SMA (20)
    try:
        vol_sma = ta.sma(volume, length=20)
        if vol_sma is not None:
            avg_vol = float(vol_sma.iloc[-1])
            curr_vol = float(volume.iloc[-1])
            out["volume_sma20"] = round(avg_vol, 2)
            out["volume_ratio"] = round(curr_vol / avg_vol, 2) if avg_vol > 0 else 1.0
    except Exception:
        out["volume_sma20"] = None
        out["volume_ratio"] = None

    # ATR (14)
    try:
        atr = ta.atr(high, low, close, length=14)
        out["atr"] = round(float(atr.iloc[-1]), 2) if atr is not None else None
    except Exception:
        out["atr"] = None

    # Current price snapshot
    out["price"]   = round(float(close.iloc[-1]),  2)
    out["prev"]    = round(float(close.iloc[-2]),  2) if len(df) > 1 else out["price"]
    out["high_20"] = round(float(high.tail(20).max()),  2)
    out["low_20"]  = round(float(low.tail(20).min()),   2)

    return out


# ── TA context string for Claude prompts ──────────────────────────────────────

def build_ta_context(coin: str, indicators: dict[str, dict]) -> str:
    """
    Format TA indicators into a compact context block for Claude prompts.
    indicators: {interval: {rsi, macd_cross, bb_position, ema9, ...}}
    Returns empty string if no data.
    """
    tf15 = indicators.get("15m", {})
    tf1h = indicators.get("1h", {})
    if not tf15 and not tf1h:
        return ""

    coin_full = COIN_FULL.get(coin, coin)
    price = tf15.get("price") or tf1h.get("price") or 0

    lines = [f"─── TECHNICAL ANALYSIS — {coin_full} ─────────────────────────"]

    def _rsi_label(v):
        if v is None:
            return "N/A"
        if v > 70:
            return f"{v} (overbought)"
        if v < 30:
            return f"{v} (oversold)"
        return str(v)

    def _ema_trend(ind):
        p = ind.get("price")
        e9 = ind.get("ema9")
        e21 = ind.get("ema21")
        if not p or not e9 or not e21:
            return "N/A"
        above9  = p > e9
        above21 = p > e21
        if above9 and above21:
            return "Above EMA9 & EMA21 (short-term bullish)"
        if not above9 and not above21:
            return "Below EMA9 & EMA21 (short-term bearish)"
        return "Between EMA9 and EMA21 (neutral)"

    def _vol_label(ratio):
        if ratio is None:
            return "N/A"
        if ratio > 1.5:
            return f"{ratio:.1f}x avg (high)"
        if ratio < 0.5:
            return f"{ratio:.1f}x avg (low)"
        return f"{ratio:.1f}x avg (normal)"

    if tf15:
        lines.append(f"15min RSI: {_rsi_label(tf15.get('rsi'))}")
        mc = tf15.get("macd_cross")
        if mc:
            lines.append(f"15min MACD: {mc.capitalize()} crossover signal")
        bb = tf15.get("bb_position")
        if bb:
            label = {"upper_band": "At upper band (potential pullback)",
                     "lower_band": "At lower band (potential bounce)",
                     "middle": "Mid-band"}.get(bb, bb)
            lines.append(f"Bollinger Bands: {label}")
        lines.append(f"EMA trend (15m): {_ema_trend(tf15)}")
        lines.append(f"Volume (15m): {_vol_label(tf15.get('volume_ratio'))}")
        atr = tf15.get("atr")
        if atr:
            lines.append(f"ATR (15m): ${atr:,.0f} (volatility measure)")

    if tf1h:
        lines.append(f"1hr RSI: {_rsi_label(tf1h.get('rsi'))}")
        mc1h = tf1h.get("macd_cross")
        if mc1h:
            lines.append(f"1hr MACD: {mc1h.capitalize()}")

    # Key levels from 1h candles
    h20 = tf1h.get("high_20") or tf15.get("high_20")
    l20 = tf1h.get("low_20") or tf15.get("low_20")
    if h20 and l20:
        lines.append(f"Recent range (20 bars): ${l20:,.0f} – ${h20:,.0f}")

    lines.append("──────────────────────────────────────────────────────────")
    return "\n".join(lines)


# ── Discord TA summary — combined Market Pulse ────────────────────────────────

def _ema_trend_short(ind: dict) -> str:
    """Returns a short trend label for the Market Pulse display."""
    p   = ind.get("price")
    e9  = ind.get("ema9")
    e21 = ind.get("ema21")
    if not p or not e9 or not e21:
        return "Unknown"
    if p > e9 and p > e21:
        return "Bullish short term"
    if p < e9 and p < e21:
        return "Bearish short term"
    return "Neutral short term"


def _ema_trend_str(ind: dict) -> str:
    """Returns a verbose trend string (used in Claude TA context)."""
    p   = ind.get("price")
    e9  = ind.get("ema9")
    e21 = ind.get("ema21")
    e50 = ind.get("ema50")
    if not p or not e9 or not e21:
        return ""
    above9  = p > e9
    above21 = p > e21
    above50 = (p > e50) if e50 else None
    if above9 and above21:
        suffix = " (above EMA50 — strong)" if above50 else ""
        return f"Above EMA9 & EMA21 — short-term bullish{suffix}"
    if not above9 and not above21:
        suffix = " (below EMA50 — weak)" if above50 is False else ""
        return f"Below EMA9 & EMA21 — short-term bearish{suffix}"
    return "Between EMA9/EMA21 — neutral"


def _rsi_label_verbose(rsi) -> str:
    """RSI value with plain-English explanation for non-traders."""
    if rsi is None:
        return "N/A"
    v = round(rsi)
    if rsi < 30:
        return f"RSI {v} (oversold — could bounce soon)"
    if rsi > 70:
        return f"RSI {v} (overbought — could pull back soon)"
    if rsi < 40:
        return f"RSI {v} (weak, approaching oversold)"
    if rsi > 60:
        return f"RSI {v} (strong, approaching overbought)"
    return f"RSI {v}"


def _macd_label_verbose(macd_cross: str) -> str:
    """MACD direction with plain-English explanation."""
    if macd_cross == "bullish":
        return "MACD bullish (momentum shifting up)"
    if macd_cross == "bearish":
        return "MACD bearish (momentum shifting down)"
    return "MACD N/A"


def _outlook_verbose(rsi, macd_cross: str, bb_pos: str) -> str:
    """Build a specific outlook sentence from the combination of signals."""
    parts = []
    if bb_pos == "lower_band":
        parts.append("At lower Bollinger Band (price at a historically low level — often bounces from here)")
    elif bb_pos == "upper_band":
        parts.append("At upper Bollinger Band (price stretched high — potential pullback zone)")
    if rsi is not None and rsi < 30:
        parts.append("RSI deeply oversold — watching for reversal")
    elif rsi is not None and rsi > 70:
        parts.append("RSI overbought — watch for exhaustion")
    if parts:
        return "; ".join(parts)
    # Fall back to MACD-based outlook
    if macd_cross == "bullish":
        return "Momentum turning positive — watching for follow-through"
    if macd_cross == "bearish":
        return "Momentum weakening — no strong setup yet"
    return "No clear setup — watching"


def build_combined_market_pulse(ta_cache: dict) -> str:
    """
    Build the combined hourly Market Pulse message for Discord #analysis.
    All three coins in a single message, with plain-English TA explanations.
    ta_cache: {coin: {interval: indicators_dict}}
    """
    import datetime
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    except (ImportError, KeyError):
        now_et = datetime.datetime.utcnow() + datetime.timedelta(hours=-4)

    h    = now_et.hour % 12 or 12
    ampm = "am" if now_et.hour < 12 else "pm"
    time_str = f"{h}:{now_et.minute:02d}{ampm}"

    sections: list[str] = []
    bullish_coins: list[str] = []
    bearish_coins: list[str] = []

    for coin in ("BTC", "ETH", "SOL"):
        coin_full  = COIN_FULL.get(coin, coin)
        indicators = ta_cache.get(coin, {})
        tf1h       = indicators.get("1h", {})
        if not tf1h:
            continue

        price     = tf1h.get("price", 0)
        rsi       = tf1h.get("rsi")
        macd      = tf1h.get("macd_cross", "")
        bb_pos    = tf1h.get("bb_position", "")
        l20       = tf1h.get("low_20")
        h20       = tf1h.get("high_20")

        # Price: whole dollars for BTC, two decimals for SOL
        price_str = f"${price:,.0f}" if price >= 100 else f"${price:.2f}"

        rsi_str  = _rsi_label_verbose(rsi)
        macd_str = _macd_label_verbose(macd)
        trend    = _ema_trend_short(tf1h)
        outlook  = _outlook_verbose(rsi, macd, bb_pos)

        support_str = f"${l20:,.0f}" if l20 else "N/A"
        resist_str  = f"${h20:,.0f}" if h20 else "N/A"

        line1 = f"**{coin_full}** {price_str} | {rsi_str} | {macd_str}"
        line2 = f"Trend: {trend} | Support {support_str} | Resistance {resist_str}"
        line3 = f"Outlook: {outlook}"

        sections.append(f"{line1}\n{line2}\n{line3}")

        if macd == "bullish":
            bullish_coins.append(coin_full)
        elif macd == "bearish":
            bearish_coins.append(coin_full)

    if not sections:
        return ""

    # Overall one-liner
    if len(bullish_coins) >= 2:
        overall = f"Crypto broadly bullish. {', '.join(bullish_coins)} showing positive momentum."
    elif len(bearish_coins) >= 2:
        overall = f"Crypto broadly bearish. {', '.join(bearish_coins)} under pressure."
    elif bullish_coins and bearish_coins:
        overall = f"Mixed signals. {bullish_coins[0]} turning up while {bearish_coins[0]} still weak."
    else:
        overall = "Momentum neutral across crypto — no clear directional bias yet."

    lines = [f"**Market Pulse — {time_str} ET**", ""]
    lines.extend(sections)
    lines += ["", f"Overall: {overall}"]
    return "\n".join(lines)


def build_discord_ta_summary(coin: str, indicators: dict[str, dict]) -> str:
    """Single-coin TA summary — kept for backwards compatibility but no longer called for hourly posts."""
    return build_combined_market_pulse({coin: indicators})


# ── Main TechnicalAnalyzer class ──────────────────────────────────────────────

class TechnicalAnalyzer:
    """
    Fetches Binance candles and maintains a cache of calculated indicators.
    Cache is keyed by (coin, interval) and refreshed on each call to refresh_all().
    """

    def __init__(self) -> None:
        # {coin: {interval: indicators_dict}}
        self._cache: dict[str, dict[str, dict]] = {}
        self._last_refresh: datetime.datetime | None = None

    async def refresh_all(self) -> dict[str, dict[str, dict]]:
        """
        Fetch candles for BTC, ETH, SOL across all intervals and recalculate
        all indicators. Updates internal cache. Returns the full cache.
        """
        results: dict[str, dict[str, dict]] = {}

        for coin, symbol in SYMBOLS.items():
            results[coin] = {}
            for interval_key in INTERVALS:
                try:
                    df = await fetch_candles(symbol, interval_key, limit=100)
                    ind = calculate_indicators(df)
                    results[coin][interval_key] = ind
                    log.debug("[ta] %s %s: price=%.2f rsi=%s", coin, interval_key,
                              ind.get("price", 0), ind.get("rsi"))
                    await asyncio.sleep(0.3)  # gentle throttle — Kraken rate limit
                except Exception as exc:
                    log.warning("[ta] failed %s %s: %s", coin, interval_key, exc)
                    results[coin][interval_key] = {}

        self._cache = results
        self._last_refresh = datetime.datetime.utcnow()
        log.info("[ta] refresh_all complete: %d coins x %d intervals",
                 len(results), len(INTERVALS))
        return results

    def get_indicators(self, coin: str) -> dict[str, dict]:
        """Return cached indicators for a coin. Empty dict if not yet fetched."""
        return self._cache.get(coin, {})

    def get_ta_context(self, coin: str) -> str:
        """Return TA context string for Claude prompt injection."""
        return build_ta_context(coin, self.get_indicators(coin))

    def get_discord_summary(self, coin: str) -> str:
        """Return human-readable TA summary for Discord #analysis."""
        return build_discord_ta_summary(coin, self.get_indicators(coin))

    def get_combined_market_pulse(self) -> str:
        """Return the combined Market Pulse string for all three coins."""
        return build_combined_market_pulse(self._cache)

    @property
    def cache_age_minutes(self) -> float:
        if not self._last_refresh:
            return 9999.0
        delta = datetime.datetime.utcnow() - self._last_refresh
        return delta.total_seconds() / 60
