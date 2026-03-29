"""
order_manager.py — Kal's order execution and tracking layer.

Handles:
  - Placing limit orders (real or demo)
  - Checking fill status
  - Cancelling stale unfilled orders (> 24h old)
"""
import asyncio
import structlog

import discord_notifier as discord
from kalshi_client import KalshiClient
from db_logger import DBLogger
from claude_client import MarketAnalysis
from config import settings

log = structlog.get_logger(__name__)


class OrderManager:
    def __init__(self, kalshi: KalshiClient, db: DBLogger) -> None:
        self.kalshi = kalshi
        self.db = db

    async def execute_trade(
        self,
        analysis: MarketAnalysis,
        decision: dict,
        ai_decision_id: str,
        close_time: str = "",
        volume: float = 0.0,
    ) -> dict | None:
        """
        Place an order based on a decision dict from DecisionEngine.
        Logs the trade to Supabase and returns the order response.
        Returns None if the trade was not placed.
        """
        if not decision["approved"]:
            log.info("trade_skipped", ticker=analysis.ticker, reason=decision["reason"])
            return None

        side = decision["side"]
        contracts = decision["contracts"]
        price = decision["price"]
        price_cents = max(1, min(99, round(price * 100)))
        bet_dollars = decision["bet_dollars"]

        log.info(
            "placing_order",
            ticker=analysis.ticker,
            side=side,
            contracts=contracts,
            price_cents=price_cents,
            bet_dollars=bet_dollars,
            demo=settings.demo_mode,
        )

        try:
            resp = await self.kalshi.place_order(
                ticker=analysis.ticker,
                side=side,
                contracts=contracts,
                price_cents=price_cents,
            )
            order = resp.get("order", resp)
            order_id = order.get("id", "unknown")

            # Log trade to DB
            await self.db.log_trade(
                ai_decision_id=ai_decision_id,
                market_id=analysis.market_id,
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                side=side,
                contracts=contracts,
                price_cents=price_cents,
                amount_dollars=bet_dollars,
                order_id=order_id,
                status="pending",
                demo_mode=settings.demo_mode,
            )

            log.info("order_placed", ticker=analysis.ticker, order_id=order_id)
            await discord.notify_trade_placed(
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                side=side,
                contracts=contracts,
                price_cents=price_cents,
                amount_dollars=bet_dollars,
                order_id=order_id,
                demo=settings.demo_mode,
                coin=analysis.coin,
                live_price=analysis.live_price,
                strike_price=analysis.strike_price,
                close_time=close_time,
                volume=volume,
            )
            return order

        except Exception as exc:
            log.error("order_failed", ticker=analysis.ticker, error=str(exc))
            await discord.notify_trade_failed(
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                error=str(exc),
            )

            # Log failed attempt
            await self.db.log_trade(
                ai_decision_id=ai_decision_id,
                market_id=analysis.market_id,
                ticker=analysis.ticker,
                market_title=analysis.market_title,
                side=side,
                contracts=contracts,
                price_cents=price_cents,
                amount_dollars=bet_dollars,
                order_id=None,
                status="failed",
                demo_mode=settings.demo_mode,
            )
            return None

    async def sync_open_orders(self) -> None:
        """
        Poll Kalshi for status of all pending orders and update DB.
        Call this at the start of each scan cycle.
        """
        try:
            orders = await self.kalshi.get_orders(status="resting")
            log.info("open_orders", count=len(orders))
            for order in orders:
                await self.db.update_trade_status(
                    order_id=order["id"],
                    status=order.get("status", "unknown"),
                    filled_contracts=order.get("filled_count", 0),
                    filled_price_cents=order.get("avg_fill_price", None),
                )
        except Exception as exc:
            log.error("sync_orders_failed", error=str(exc))

    async def cancel_stale_orders(self, max_age_hours: int = 24) -> None:
        """Cancel unfilled orders older than max_age_hours."""
        try:
            stale = await self.db.get_stale_orders(max_age_hours=max_age_hours)
            for row in stale:
                order_id = row["order_id"]
                if not order_id:
                    continue
                if order_id.startswith("demo-"):
                    # Paper/demo trades don't have real Kalshi orders — just expire them
                    # so they don't accumulate as "open positions" forever.
                    await self.db.update_trade_status(order_id=order_id, status="cancelled")
                    log.info("expired_demo_trade", order_id=order_id)
                else:
                    await self.kalshi.cancel_order(order_id)
                    await self.db.update_trade_status(order_id=order_id, status="cancelled")
                    log.info("cancelled_stale_order", order_id=order_id)
        except Exception as exc:
            log.error("cancel_stale_failed", error=str(exc))
