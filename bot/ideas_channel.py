"""
ideas_channel.py — Kal's private ideas channel.

Kal posts here when he spots opportunities OUTSIDE his current trading mandate.
He cannot execute these — he flags them for the owner's decision.

Rules:
  - Max ONE idea post per day
  - Only genuine high conviction setups — no noise
  - Kal executes Kalshi + crypto autonomously (never posts those here)
  - Only stocks, bonds, commodities, and mandate expansions need approval

Types Kal flags:
  - Stock or sector ETF opportunity
  - Bond / rates trade
  - Commodity (oil, gold)
  - New Kalshi market category Kal wants permission to trade
  - Risk limit expansion request

ONE Claude call per idea. Cost budget: max 1 call/day.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parent / "ideas_state.json"


# ── State ─────────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("[ideas] state write failed: %s", exc)


def already_posted_today() -> bool:
    state = _read_state()
    return state.get("posted_date") == datetime.date.today().isoformat()


def mark_posted() -> None:
    state = _read_state()
    state["posted_date"] = datetime.date.today().isoformat()
    _write_state(state)


# ── Idea types ────────────────────────────────────────────────────────────────

IDEA_TYPES = {
    "stock":       "Stock",
    "bond":        "Bond",
    "commodity":   "Commodity",
    "new_market":  "New Kalshi market",
    "risk_limit":  "Risk limit request",
}

# Signals in intelligence alerts that suggest non-mandate opportunities
_STOCK_SIGNALS  = ["s&p", "nasdaq", "spy", "qqq", "sector", "etf", "equity", "earnings",
                    "tech", "energy", "financial", "healthcare", "consumer"]
_BOND_SIGNALS   = ["treasury", "yield", "bond", "rate", "tlt", "duration", "fed funds",
                   "curve", "spread", "tlt", "hy spread"]
_CMDTY_SIGNALS  = ["gold", "oil", "crude", "wti", "brent", "silver", "copper", "gas", "gld"]


def _classify_signal(text: str) -> str | None:
    """Classify a free-text signal into an idea type. Returns None if not relevant."""
    t = text.lower()
    if any(kw in t for kw in _STOCK_SIGNALS):
        return "stock"
    if any(kw in t for kw in _BOND_SIGNALS):
        return "bond"
    if any(kw in t for kw in _CMDTY_SIGNALS):
        return "commodity"
    return None


# ── Claude-powered idea generation ───────────────────────────────────────────

IDEA_SYSTEM_PROMPT = """\
You are Kal, an AI trader who has spotted an opportunity OUTSIDE your current trading mandate.
Your current mandate: Kalshi prediction markets (crypto) and crypto spot trades (BTC/ETH/SOL).

You are flagging an idea in the #ideas channel for the server owner to evaluate.
The owner will reply APPROVED or PASS.

Rules:
- Only post if you have genuine high conviction — not noise
- Be specific: name the exact instrument, entry level, target, timeframe
- Connect everything to the bond/macro context — bond market leads everything
- Be honest about risks
- This is a private channel — write like you're briefing a sophisticated trader

Respond ONLY with the formatted idea post. No preamble. Start with "**Idea --".
"""

IDEA_USER_TEMPLATE = """\
Signal context:
{signal_context}

Bond/macro data:
{bond_context}

Today's news:
{news_context}

Write the full idea post in this exact format:

**Idea -- [Brief Title]**
Type: [Stock / Bond / Commodity / New Kalshi market / Risk limit request]
Time sensitive: Yes ([why]) / No
Conviction: High / Medium

What I see:
[2-3 sentences -- specific prices, yields, events driving this]

The connection:
[How this connects to bonds, macro, news today]

The trade:
[Exactly what I recommend -- instrument, direction, entry, target, timeframe]

Risk:
[What could go wrong -- be honest]

Note: Outside my current mandate. Your call.
Reply APPROVED or PASS.
"""


async def evaluate_for_ideas(
    signal_text: str,
    fred_data: dict,
    news_headlines: list[str],
    anthropic_api_key: str,
    claude_model: str,
    model_override: str | None = None,
) -> tuple[str | None, float]:
    """
    Evaluate whether a signal warrants an #ideas post.
    Returns (idea_text, cost_dollars) or (None, 0.0) if not worth posting.

    Only called when:
      1. Signal is NOT Kalshi or crypto (those execute autonomously)
      2. Conviction threshold is high enough to bother the owner
      3. We haven't already posted an idea today (max 1/day)
    """
    if already_posted_today():
        log.debug("[ideas] already posted today — skipping evaluation")
        return None, 0.0

    # Quick pre-check: does the signal even look like a non-mandate opportunity?
    idea_type = _classify_signal(signal_text)
    if idea_type is None:
        return None, 0.0

    # Build contexts
    from fred_client import FredClient
    fred = FredClient("")
    bond_ctx  = fred.build_rotational_context(fred_data) if fred_data else "Bond data unavailable."
    news_ctx  = "\n".join(f"- {h}" for h in news_headlines[:5]) if news_headlines else "No headlines."

    user_msg = IDEA_USER_TEMPLATE.format(
        signal_context=signal_text[:800],
        bond_context=bond_ctx,
        news_context=news_ctx,
    )

    model = model_override or claude_model
    import time
    t0 = time.time()
    cost = 0.0
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=IDEA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        inp  = response.usage.input_tokens
        out  = response.usage.output_tokens
        cost = inp * 0.000003 + out * 0.000015
        log.info("[ideas] idea generated tokens=%d+%d cost=$%.4f ms=%d",
                 inp, out, cost, round((time.time()-t0)*1000))

        # Quick sanity check — Claude must have generated a real idea
        if "**Idea --" not in text and "**Idea -" not in text:
            log.debug("[ideas] claude output didn't look like an idea post — discarding")
            return None, cost

        return text, cost

    except Exception as exc:
        log.warning("[ideas] claude call failed: %s", exc)
        return None, 0.0
