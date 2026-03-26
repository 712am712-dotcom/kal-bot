"""
claude_client.py — Kal's Claude integration for crypto price market analysis.

Specialized for short-term BTC/ETH/SOL price prediction markets on Kalshi.
Every call receives live spot prices + 24h momentum from CoinGecko so
Claude always reasons from current market conditions, not stale training data.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field

import anthropic

from config import settings

log = logging.getLogger(__name__)


@dataclass
class MarketAnalysis:
    market_id: str
    ticker: str
    market_title: str
    yes_probability: float       # Claude's estimate
    kalshi_yes_price: float      # Current Kalshi price
    edge: float                  # yes_probability - kalshi_yes_price
    confidence: float
    reasoning: str
    recommendation: str          # BUY_YES | BUY_NO | SKIP
    tokens_used: int
    latency_ms: int
    # Crypto-specific (populated when market_focus = "crypto")
    coin: str = ""               # BTC | ETH | SOL
    timeframe: str = ""          # 15min | 1hr | daily
    live_price: float = 0.0      # spot price at analysis time
    strike_price: float = 0.0    # the threshold in the market title
    direction: str = ""          # ABOVE | BELOW
    # Cost tracking
    input_tokens: int = 0
    output_tokens: int = 0
    cost_dollars: float = 0.0    # estimated API cost for this call
    model_used: str = ""         # which model actually ran


# ── Prompts ───────────────────────────────────────────────────────────────────

# Kal's permanent identity — prepended to every system prompt.
# Import this in other modules: from claude_client import KAL_IDENTITY
KAL_IDENTITY = """\
You are Kal — an AI financial intelligence system and the operating brain of a growing AI holding company. \
You were built to find opportunity, execute with precision, learn from every outcome, \
and get measurably better every single day.

Your current mandate:
- Trade Kalshi prediction markets and crypto autonomously within defined risk parameters
- Read markets across all asset classes — bonds, crypto, stocks, commodities, prediction markets
- Synthesize morning intelligence from 5 financial newsletters daily
- Reflect on every trade and market move — what you got right, what you missed, why
- Flag opportunities outside your mandate to your CEO for approval
- Think in rotations — bonds tell you everything, macro tells you where, best instrument tells you how

Your operating philosophy:
- Bonds first. What are rates telling you? This leads everything else.
- Follow the money. Where are institutions rotating? Why?
- Find the best expression. Kalshi, crypto, stocks, commodities — always ask what is the best trade.
- Quality over quantity. One high conviction trade beats ten mediocre ones.
- Compound everything. Every trade, every lesson, every day builds on the last.
- Be accountable. If you were wrong, say why. If you were right, say why. Never hide from outcomes.

Your growth mission:
You are not just a trading bot. You are the foundation of an AI holding company that will eventually:
- Run multiple trading operations simultaneously
- Spot content and business opportunities and delegate to specialized agents
- Generate enough passive income to fund its own expansion
- Become a system that businesses pay to license — Kal's Mind as a product

Today is always the lowest point you will ever be at. Every day you are 1% better than yesterday. \
You stack. You compound. You build.

Your CEO checks in daily. You report honestly. You flag ideas. You execute within your mandate. \
You never stop scanning for opportunity. This is not a job. This is a mission.
"""


CRYPTO_SYSTEM_PROMPT = KAL_IDENTITY + """
Your current task: Estimate the TRUE probability that a Kalshi crypto price prediction market \
resolves YES, given live spot prices, recent momentum, and short-term price dynamics.

Analysis principles:
- You have real-time spot prices. Use them as your anchor.
- For 15-minute markets: price moves of >0.5% are rare. Mean-reversion is strong.
- For 1-hour markets: trend continuation slightly outweighs mean reversion.
- For daily markets: trend + macro regime matter most.
- The Kalshi implied probability IS the crowd's estimate — find where the crowd is wrong.
- Be calibrated: 70% confidence means you expect to be right ~70% of the time.
- If the market is within 0.5% of the strike, widen your uncertainty.

Rotational thinking — always factor the macro regime:
- The bond market leads everything else. Rising yields = headwind for risk assets.
- If the 10Y-2Y yield curve is inverted, recession risk is elevated — widen uncertainty on bullish crypto calls.
- When Fed Funds Rate is high and credit spreads are wide, risk-off bias applies to crypto.
- When high yield spreads tighten and yields are stable, crypto has a better tailwind.
- Dollar strength (when Fed hikes) typically pressures Bitcoin and crypto short-term.
- Always ask: is this thesis better expressed as Kalshi or crypto spot? Flag stocks/bonds/commodities to #ideas.

Reasoning style:
- Be specific to THIS market — mention the actual distance from strike, the actual RSI value,
  the actual candle context, the actual volume pattern if unusual.
- Never use generic phrases like "coin flip", "24h downtrend", "mean reversion is the default",
  or "noisy short-term market" — these say nothing specific and apply to every market equally.
- Every reasoning field must tell the reader something they couldn't infer from base rates alone.
- If RSI or MACD shaped your estimate, say so explicitly and say how.
- If the edge comes purely from distance-to-strike math, say exactly what distance and why it matters.
- If bond/macro context (yield curve, Fed rate, HY spread) is provided, factor it explicitly.

Respond ONLY with valid JSON — no markdown, no preamble, no explanation outside the JSON.
"""

CRYPTO_ANALYSIS_PROMPT = """\
Analyze this Kalshi crypto price prediction market:

─── LIVE MARKET CONDITIONS ───────────────────────────────
{live_prices_block}
─── MARKET DETAILS ───────────────────────────────────────
Title:        {title}
Ticker:       {ticker}
Coin:         {coin}
Timeframe:    {timeframe}
Direction:    Will {coin} close {direction} ${strike_price:,.2f}?
Close Time:   {close_time}  (resolves in ~{minutes_to_close:.0f} minutes)

─── KALSHI PRICING ───────────────────────────────────────
Current YES price (crowd implied prob): {yes_price:.1%}
Current NO  price:                      {no_price:.1%}
Market Volume:  ${volume:,.0f}

─── YOUR TASK ────────────────────────────────────────────
Estimate the TRUE probability this market resolves YES.
Consider:
  1. Distance from current spot to strike: {distance_pct:+.2f}% ({direction_from_spot})
  2. Time remaining vs typical volatility for this timeframe
  3. Recent 24h momentum: {coin} is {change_24h:+.2f}% in the last 24h
  4. Whether the crowd's implied probability looks right, too high, or too low

Respond with this exact JSON:
{{
  "yes_probability": <float 0.0–1.0>,
  "confidence":      <float 0.0–1.0>,
  "reasoning":       "<2–3 sentences explaining your estimate vs the crowd>",
  "key_factors":     ["<factor 1>", "<factor 2>", "<factor 3>"],
  "crowd_bias":      "<overpriced_yes | overpriced_no | fairly_priced>"
}}
"""

DIRECTIONAL_ANALYSIS_PROMPT = """\
Analyze this Kalshi directional crypto market:

─── LIVE MARKET CONDITIONS ───────────────────────────────
{live_prices_block}
─── MARKET DETAILS ───────────────────────────────────────
Title:        {title}
Ticker:       {ticker}
Coin:         {coin}
Timeframe:    {timeframe}
Question:     Will {coin} price be HIGHER at close than it is right now?
Close Time:   {close_time}  (resolves in ~{minutes_to_close:.0f} minutes)

─── KALSHI PRICING ───────────────────────────────────────
Current YES price (crowd says YES = price goes UP): {yes_price:.1%}
Current NO  price (crowd says price stays flat/down): {no_price:.1%}
Market Volume:  ${volume:,.0f}

─── YOUR TASK ────────────────────────────────────────────
Estimate the TRUE probability this market resolves YES (i.e., {coin} closes higher).
Consider:
  1. Short-term momentum: {coin} is {change_24h:+.2f}% in the last 24h
  2. For 15-minute windows: moves above 0.3% are uncommon; mean-reversion is the default
  3. Whether the crowd's implied {yes_price:.1%} looks right, too high, or too low
  4. Bid-ask spread suggests crowd uncertainty — this is a noisy market

Respond with this exact JSON:
{{
  "yes_probability": <float 0.0–1.0>,
  "confidence":      <float 0.0–1.0>,
  "reasoning":       "<2–3 sentences explaining your estimate vs the crowd>",
  "key_factors":     ["<factor 1>", "<factor 2>", "<factor 3>"],
  "crowd_bias":      "<overpriced_yes | overpriced_no | fairly_priced>"
}}
"""

# Fallback prompt for non-crypto markets
GENERAL_ANALYSIS_PROMPT = """\
Analyze this Kalshi prediction market:

Title: {title}
Category: {category}
Close Time: {close_time}
Current YES price (implied probability): {yes_price:.1%}
Current NO price: {no_price:.1%}
Volume (USD): ${volume:,.0f}

Estimate the TRUE probability this market resolves YES.

Respond with this exact JSON:
{{
  "yes_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentence explanation>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"]
}}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_coin(ticker: str, title: str) -> str:
    t = (ticker + " " + title).upper()
    if "BTC" in t or "BITCOIN" in t:
        return "BTC"
    if "ETH" in t or "ETHEREUM" in t:
        return "ETH"
    if "SOL" in t or "SOLANA" in t:
        return "SOL"
    return "BTC"  # fallback


def _detect_timeframe(ticker: str, title: str, close_time: str) -> str:
    """Infer 15min / 1hr / daily from ticker, title, or time-to-close."""
    combined = (ticker + " " + title).upper()
    if "15M" in combined or "15MIN" in combined:
        return "15min"
    if "1H" in combined or "1HR" in combined or "HOURLY" in combined:
        return "1hr"
    if "DAILY" in combined or combined.endswith("D-") or "KXBTCD" in combined:
        return "daily"
    # Fall back: infer from close_time
    try:
        import datetime
        ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        mins = (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
        if mins <= 20:
            return "15min"
        if mins <= 90:
            return "1hr"
    except Exception:
        pass
    return "daily"


def _extract_strike(title: str) -> float:
    """Pull the dollar threshold from a title like 'Bitcoin above $95,500 on Jan 15'."""
    match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", title)
    if match:
        return float(match.group(1).replace(",", ""))
    return 0.0


def _detect_direction(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ("above", "over", "exceed", "higher", "at least")):
        return "ABOVE"
    if any(w in t for w in ("below", "under", "lower", "beneath")):
        return "BELOW"
    return "ABOVE"


def _is_directional_market(title: str) -> bool:
    """True for markets like 'BTC price up in next 15 mins?' — no dollar strike."""
    t = title.lower()
    return "$" not in t and any(w in t for w in ("up", "down", "higher", "lower", "increase", "decrease", "rise", "fall"))


def _minutes_to_close(close_time: str) -> float:
    try:
        import datetime
        ct = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        delta = (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
        return max(0.0, delta)
    except Exception:
        return 60.0


def _build_live_prices_block(crypto_prices: dict, coin: str) -> str:
    """Format live prices for the prompt. Emphasizes the relevant coin."""
    lines = []
    for symbol, data in crypto_prices.items():
        price     = data.get("price", 0)
        chg_24h   = data.get("change_24h", 0)
        arrow     = "▲" if chg_24h >= 0 else "▼"
        marker    = " ◄ THIS MARKET" if symbol == coin else ""
        lines.append(
            f"  {symbol:3s}: ${price:>12,.2f}  {arrow} {abs(chg_24h):.2f}% (24h){marker}"
        )
    return "\n".join(lines)


# ── Client ────────────────────────────────────────────────────────────────────

class ClaudeClient:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = settings.anthropic_api_key
            if not key or not key.startswith("sk-ant-"):
                raise ValueError(f"ANTHROPIC_API_KEY looks wrong — got: {key[:12]}...")
            log.debug(f"[claude] initialising client key={key[:16]}…")
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def analyze_market(
        self,
        market_id: str,
        ticker: str,
        title: str,
        category: str,
        yes_price: float,
        no_price: float,
        volume: float,
        close_time: str,
        crypto_prices: dict | None = None,  # {BTC: {price, change_24h}, ETH: …, SOL: …}
        model_override: str | None = None,  # pass fallback model when daily limit hit
        ta_context: str = "",               # pre-formatted TA block from TechnicalAnalyzer
    ) -> MarketAnalysis:
        """
        Analyze one market. If crypto_prices is provided (and market_focus=crypto),
        uses the specialized crypto prompt with live price context.
        Raises on API or parse errors.
        """
        is_crypto = (
            settings.market_focus == "crypto"
            and crypto_prices is not None
        )

        # ── Build prompt ──────────────────────────────────────────────────────
        if is_crypto:
            coin       = _detect_coin(ticker, title)
            timeframe  = _detect_timeframe(ticker, title, close_time)
            strike     = _extract_strike(title)
            direction  = _detect_direction(title)
            mins_left  = _minutes_to_close(close_time)
            live_data  = crypto_prices.get(coin, {})
            live_price = live_data.get("price", 0.0)
            change_24h = live_data.get("change_24h", 0.0)

            # ── Pre-call sanity checks ────────────────────────────────────
            # Return SKIP immediately without spending an API call when the
            # market is clearly a placeholder or has no meaningful data.

            def _skip(reason: str) -> "MarketAnalysis":
                return MarketAnalysis(
                    market_id=market_id,
                    ticker=ticker,
                    market_title=title,
                    yes_probability=yes_price,
                    kalshi_yes_price=yes_price,
                    edge=0.0,
                    confidence=0.0,
                    reasoning=f"Auto-skipped: {reason}",
                    recommendation="SKIP",
                    tokens_used=0,
                    latency_ms=0,
                    coin=coin,
                    timeframe=timeframe,
                    live_price=live_price,
                    strike_price=strike,
                    direction=direction,
                )

            is_directional = _is_directional_market(title)

            if strike == 0.0 and not is_directional:
                log.debug("[claude] skipping — no strike price in title", ticker=ticker)
                return _skip("no strike price detected in market title")

            if yes_price == 0.50 and volume == 0.0:
                log.debug("[claude] skipping — 50/50 placeholder with zero volume", ticker=ticker)
                return _skip("50/50 placeholder market with zero volume")

            if yes_price in (0.01, 0.99) and volume == 0.0:
                log.debug("[claude] skipping — extreme price with zero volume", ticker=ticker)
                return _skip("extreme default price with zero volume")

            if is_directional:
                # ── Directional market (e.g. "BTC price up in next 15 mins?") ──
                prompt = DIRECTIONAL_ANALYSIS_PROMPT.format(
                    live_prices_block=_build_live_prices_block(crypto_prices, coin),
                    title=title,
                    ticker=ticker,
                    coin=coin,
                    timeframe=timeframe,
                    close_time=close_time,
                    minutes_to_close=mins_left,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    change_24h=change_24h,
                )
                system = CRYPTO_SYSTEM_PROMPT
            else:
                # ── Level-based market (e.g. "Will BTC close above $70,300?") ──
                if strike > 0 and live_price > 0:
                    distance_pct      = ((live_price - strike) / strike) * 100
                    direction_from_spot = (
                        f"spot is {abs(distance_pct):.2f}% "
                        + ("above" if live_price >= strike else "below")
                        + " strike"
                    )
                else:
                    distance_pct       = 0.0
                    direction_from_spot = "strike not detected"

                prompt = CRYPTO_ANALYSIS_PROMPT.format(
                    live_prices_block=_build_live_prices_block(crypto_prices, coin),
                    title=title,
                    ticker=ticker,
                    coin=coin,
                    timeframe=timeframe,
                    direction=direction.lower(),
                    strike_price=strike,
                    close_time=close_time,
                    minutes_to_close=mins_left,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    distance_pct=distance_pct,
                    direction_from_spot=direction_from_spot,
                    change_24h=change_24h,
                )
                system = CRYPTO_SYSTEM_PROMPT

            # ── Inject TA context if available ────────────────────────────
            if ta_context:
                prompt = prompt + "\n\n" + ta_context
        else:
            coin = timeframe = direction = ""
            strike = live_price = 0.0
            prompt = GENERAL_ANALYSIS_PROMPT.format(
                title=title,
                category=category,
                close_time=close_time,
                yes_price=yes_price,
                no_price=no_price,
                volume=volume,
            )
            system = (
                KAL_IDENTITY + "\n"
                "Your current task: Estimate the TRUE probability this prediction market resolves YES. "
                "Apply rotational thinking — check what bonds and macro are saying before committing to a probability. "
                "Respond ONLY with valid JSON — no markdown, no preamble."
            )

        # ── Call API ──────────────────────────────────────────────────────────
        active_model = model_override or settings.claude_model
        start  = time.monotonic()
        client = self._get_client()
        log.debug(f"[claude] {ticker} | yes={yes_price:.1%} | coin={coin or 'N/A'} | tf={timeframe or 'N/A'} | model={active_model}")
        message = client.messages.create(
            model=active_model,
            max_tokens=settings.claude_max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        # ── Parse response ────────────────────────────────────────────────────
        raw = message.content[0].text.strip()

        def _parse_json(text: str) -> dict:
            """Try direct parse, then extract first {...} block, then retry API once."""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            # Claude sometimes adds prose before/after the JSON — grab the first object
            match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            # Broader: find outermost { ... }
            start = text.find("{")
            end   = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Claude returned unparseable JSON: {text[:300]}")

        try:
            parsed = _parse_json(raw)
        except ValueError:
            # One retry — ask Claude again for the same market
            log.warning("[claude] json_parse_failed_retrying", ticker=ticker, raw_preview=raw[:120])
            retry_msg = client.messages.create(
                model=active_model,
                max_tokens=settings.claude_max_tokens,
                system=system,
                messages=[
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": raw},
                    {"role": "user",      "content": "Your response was not valid JSON. Reply with ONLY the JSON object, no other text."},
                ],
            )
            raw = retry_msg.content[0].text.strip()
            parsed = _parse_json(raw)  # raises ValueError → caught upstream as skip

        yes_prob   = float(parsed["yes_probability"])
        confidence = float(parsed["confidence"])
        reasoning  = parsed.get("reasoning", "")
        key_factors = parsed.get("key_factors", [])
        crowd_bias  = parsed.get("crowd_bias", "")
        if key_factors:
            reasoning += " | " + "; ".join(key_factors)
        if crowd_bias:
            reasoning += f" | Crowd bias: {crowd_bias}"

        edge = yes_prob - yes_price

        # ── Recommendation — use tighter thresholds for crypto ─────────────
        if settings.paper_trading:
            min_edge = settings.paper_min_edge
            min_conf = settings.paper_min_confidence
        elif is_crypto:
            min_edge = settings.crypto_min_edge
            min_conf = settings.crypto_min_confidence
        else:
            min_edge = settings.min_edge
            min_conf = settings.min_confidence

        if abs(edge) < min_edge or confidence < min_conf:
            recommendation = "SKIP"
        elif edge > 0:
            recommendation = "BUY_YES"
        else:
            recommendation = "BUY_NO"

        in_tok  = message.usage.input_tokens
        out_tok = message.usage.output_tokens
        # Cost per token: Opus $15/$75 per M; Haiku $0.80/$4 per M
        if "haiku" in active_model.lower():
            cost = in_tok * 0.80 / 1_000_000 + out_tok * 4.0 / 1_000_000
        else:
            cost = in_tok * 15.0 / 1_000_000 + out_tok * 75.0 / 1_000_000

        return MarketAnalysis(
            market_id=market_id,
            ticker=ticker,
            market_title=title,
            yes_probability=yes_prob,
            kalshi_yes_price=yes_price,
            edge=edge,
            confidence=confidence,
            reasoning=reasoning,
            recommendation=recommendation,
            tokens_used=in_tok + out_tok,
            latency_ms=latency_ms,
            coin=coin,
            timeframe=timeframe,
            live_price=live_price,
            strike_price=strike,
            direction=direction,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_dollars=round(cost, 6),
            model_used=active_model,
        )
