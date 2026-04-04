"""
decision_engine.py — Kal's risk engine. Applies all controls to a
MarketAnalysis and decides whether to place a trade.
"""
import structlog

import discord_notifier as discord
from claude_client import MarketAnalysis
from config import settings

log = structlog.get_logger(__name__)


class RiskViolation(Exception):
    """Raised when a trade is blocked by a risk control."""
    pass


class DecisionEngine:
    def __init__(self, current_positions: int, daily_loss_dollars: float) -> None:
        """
        current_positions: number of currently open positions
        daily_loss_dollars: total dollars lost today (positive number)
        """
        self.current_positions = current_positions
        self.daily_loss_dollars = daily_loss_dollars

    def evaluate(self, analysis: MarketAnalysis, volume: float) -> dict:
        """
        Evaluate a MarketAnalysis against all risk controls.

        Returns a decision dict:
        {
          "approved": bool,
          "reason": str,
          "side": "yes" | "no" | None,
          "bet_dollars": float,
          "price": float,           # Kalshi price of the chosen side (0.0–1.0)
          "contracts": int,
        }
        """
        decision: dict = {
            "approved": False,
            "reason": "",
            "side": None,
            "bet_dollars": 0.0,
            "price": 0.0,
            "contracts": 0,
        }

        log.info(
            "decision_engine_start",
            ticker=analysis.ticker,
            crowd_price=f"{analysis.kalshi_yes_price:.1%}",
            kal_estimate=f"{analysis.yes_probability:.1%}",
            edge=f"{analysis.edge:+.1%}",
            confidence=f"{analysis.confidence:.0%}",
            recommendation=analysis.recommendation,
        )

        def _skip(reason: str) -> dict:
            decision["reason"] = reason
            log.info("decision_engine_result", ticker=analysis.ticker, action="SKIP", reason=reason)
            return decision

        # Research mode — never place real trades
        if settings.research_mode:
            return _skip("research_mode")

        # Demo mode guard (extra safety)
        if settings.demo_mode:
            log.debug("demo_mode_active", ticker=analysis.ticker)
            # In demo mode we still evaluate but flag it — order_manager will simulate

        # Use tighter crypto thresholds when in crypto focus mode;
        # paper trading uses looser thresholds to capture more data
        is_crypto = bool(getattr(analysis, "coin", ""))
        if settings.paper_trading:
            min_edge = settings.paper_min_edge
            min_conf = settings.paper_min_confidence
        elif is_crypto:
            min_edge = settings.crypto_min_edge
            min_conf = settings.crypto_min_confidence
        else:
            min_edge = settings.min_edge
            min_conf = settings.min_confidence

        # ── Placeholder market guards ────────────────────────────────────────
        # These should have been filtered upstream, but defence-in-depth.
        # Require zero volume too: directional markets ("BTC price up?") have no
        # strike price but DO have real volume — they're not placeholders.
        if is_crypto and getattr(analysis, "strike_price", 0.0) == 0.0 and volume == 0:
            return _skip("no_strike_price (placeholder market)")

        if analysis.kalshi_yes_price == 0.50 and volume == 0:
            return _skip("placeholder_50_50_zero_volume")

        # ── Confidence threshold ─────────────────────────────────────────────
        if analysis.confidence < min_conf:
            return _skip(f"confidence_too_low ({analysis.confidence:.0%} < {min_conf:.0%})")

        # ── Edge threshold ───────────────────────────────────────────────────
        if abs(analysis.edge) < min_edge:
            return _skip(f"edge_too_small ({abs(analysis.edge):.1%} < {min_edge:.1%})")

        # ── Liquidity ────────────────────────────────────────────────────────
        if not settings.paper_trading and volume < settings.min_liquidity_dollars:
            return _skip(f"insufficient_liquidity (${volume:,.0f} < ${settings.min_liquidity_dollars:,.0f})")

        # ── Daily loss limit ─────────────────────────────────────────────────
        if self.daily_loss_dollars >= settings.daily_loss_limit_dollars:
            reason = f"daily_loss_limit_hit (${self.daily_loss_dollars:.2f} >= ${settings.daily_loss_limit_dollars:.2f})"
            import asyncio
            asyncio.create_task(discord.notify_error(
                context="Daily loss limit hit — trading halted for today",
                error=reason,
                critical=False,
            ))
            return _skip(reason)

        # ── Max open positions ───────────────────────────────────────────────
        if self.current_positions >= settings.max_open_positions:
            return _skip(f"max_positions_reached ({self.current_positions}/{settings.max_open_positions})")

        # ── Compute order parameters ─────────────────────────────────────────
        if analysis.recommendation == "BUY_YES":
            side = "yes"
            price = analysis.kalshi_yes_price
        elif analysis.recommendation == "BUY_NO":
            side = "no"
            price = 1.0 - analysis.kalshi_yes_price
        else:
            return _skip("claude_recommended_skip")

        # Bet sizing: fixed at max_bet, capped so we can fill at least 1 contract
        price_cents = max(1, min(99, round(price * 100)))
        bet_dollars = min(settings.max_bet_dollars, price_cents / 100 * 1000)  # sanity max
        contracts = max(1, int((bet_dollars * 100) / price_cents))
        actual_cost = contracts * price_cents / 100

        decision.update(
            approved=True,
            reason="all_checks_passed",
            side=side,
            bet_dollars=actual_cost,
            price=price,
            contracts=contracts,
        )
        log.info(
            "decision_engine_result",
            ticker=analysis.ticker,
            action="TRADE",
            side=side,
            contracts=contracts,
            bet_dollars=f"${actual_cost:.2f}",
            reason="all_checks_passed",
        )
        return decision
