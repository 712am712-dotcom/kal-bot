"""
db_logger.py — All Supabase writes (and some reads) for Kal.

Uses the Supabase Python client with the service role key so RLS is bypassed
on the server side. All tables use UUIDs as primary keys.
"""
from __future__ import annotations

import datetime
from typing import Any

import structlog
from supabase import create_client, Client

from config import settings

log = structlog.get_logger(__name__)


class DBLogger:
    def __init__(self) -> None:
        self._client: Client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )

    # ── Markets Log ──────────────────────────────────────────────────────────

    async def log_market_scan(
        self,
        market_id: str,
        ticker: str,
        title: str,
        category: str | None,
        yes_price: float,
        no_price: float,
        volume: float,
        open_interest: float,
        close_time: str | None,
        scan_mode: str,
    ) -> str:
        """Insert a row into markets_log. Returns the new row id."""
        row = {
            "market_id": market_id,
            "ticker": ticker,
            "title": title,
            "category": category,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": volume,
            "open_interest": open_interest,
            "close_time": close_time,
            "scan_mode": scan_mode,
        }
        resp = self._client.table("markets_log").insert(row).execute()
        return resp.data[0]["id"] if resp.data else ""

    # ── AI Decisions ─────────────────────────────────────────────────────────

    async def log_ai_decision(
        self,
        market_id: str,
        ticker: str,
        market_title: str,
        claude_yes_probability: float,
        kalshi_yes_price: float,
        edge: float,
        confidence: float,
        reasoning: str,
        recommendation: str,
        decision_mode: str,
        tokens_used: int,
        latency_ms: int,
    ) -> str:
        """Insert a row into ai_decisions. Returns the new row id."""
        row = {
            "market_id": market_id,
            "ticker": ticker,
            "market_title": market_title,
            "claude_yes_probability": claude_yes_probability,
            "kalshi_yes_price": kalshi_yes_price,
            "edge": edge,
            "confidence": confidence,
            "reasoning": reasoning,
            "recommendation": recommendation,
            "decision_mode": decision_mode,
            "tokens_used": tokens_used,
            "latency_ms": latency_ms,
        }
        resp = self._client.table("ai_decisions").insert(row).execute()
        return resp.data[0]["id"] if resp.data else ""

    # ── Trades ───────────────────────────────────────────────────────────────

    async def log_trade(
        self,
        ai_decision_id: str,
        market_id: str,
        ticker: str,
        market_title: str,
        side: str,
        contracts: int,
        price_cents: int,
        amount_dollars: float,
        order_id: str | None,
        status: str,
        demo_mode: bool,
    ) -> str:
        row = {
            "ai_decision_id": ai_decision_id,
            "market_id": market_id,
            "ticker": ticker,
            "market_title": market_title,
            "side": side,
            "contracts": contracts,
            "price_cents": price_cents,
            "amount_dollars": amount_dollars,
            "order_id": order_id,
            "status": status,
            "demo_mode": demo_mode,
        }
        resp = self._client.table("trades").insert(row).execute()
        return resp.data[0]["id"] if resp.data else ""

    async def update_trade_status(
        self,
        order_id: str,
        status: str,
        filled_contracts: int | None = None,
        filled_price_cents: int | None = None,
        pnl_dollars: float | None = None,
        resolution: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }
        if filled_contracts is not None:
            updates["filled_contracts"] = filled_contracts
        if filled_price_cents is not None:
            updates["filled_price_cents"] = filled_price_cents
        if pnl_dollars is not None:
            updates["pnl_dollars"] = pnl_dollars
        if resolution is not None:
            updates["resolution"] = resolution
            updates["resolved_at"] = datetime.datetime.utcnow().isoformat()

        self._client.table("trades").update(updates).eq("order_id", order_id).execute()

    async def get_stale_orders(self, max_age_hours: int = 24) -> list[dict]:
        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=max_age_hours)
        ).isoformat()
        resp = (
            self._client.table("trades")
            .select("id, order_id")
            .eq("status", "pending")
            .lt("created_at", cutoff)
            .execute()
        )
        return resp.data or []

    # ── Daily Loss ────────────────────────────────────────────────────────────

    async def get_today_loss(self) -> float:
        """Sum of negative P&L from filled trades today."""
        today = datetime.date.today().isoformat()
        resp = (
            self._client.table("trades")
            .select("pnl_dollars")
            .eq("status", "filled")
            .gte("created_at", today)
            .lt("pnl_dollars", 0)
            .execute()
        )
        rows = resp.data or []
        return abs(sum(r["pnl_dollars"] for r in rows if r["pnl_dollars"] is not None))

    async def get_open_position_count(self) -> int:
        """Count REAL open positions only — excludes demo/paper trades."""
        resp = (
            self._client.table("trades")
            .select("id", count="exact")
            .in_("status", ["pending", "partial"])
            .eq("demo_mode", False)
            .execute()
        )
        return resp.count or 0

    # ── Bot Config ────────────────────────────────────────────────────────────

    async def get_config(self, key: str) -> str | None:
        resp = self._client.table("bot_config").select("value").eq("key", key).single().execute()
        return resp.data["value"] if resp.data else None

    async def set_config(self, key: str, value: str) -> None:
        self._client.table("bot_config").upsert(
            {"key": key, "value": value, "updated_at": datetime.datetime.utcnow().isoformat()},
            on_conflict="key",
        ).execute()

    # ── Daily Summary ─────────────────────────────────────────────────────────

    async def upsert_daily_summary(self, summary: dict) -> None:
        self._client.table("daily_summary").upsert(
            summary, on_conflict="date"
        ).execute()

    # ── Strategy Report ───────────────────────────────────────────────────────

    async def save_strategy_report(self, report: dict) -> str:
        resp = self._client.table("strategy_report").insert(report).execute()
        return resp.data[0]["id"] if resp.data else ""

    # ── Content Jobs ──────────────────────────────────────────────────────────

    async def queue_content_job(
        self,
        brand: str,
        template: str,
        content: dict,
        renderer: str = "remotion",
        format: str = "vertical_30s",
    ) -> str:
        """Insert a row into content_jobs with status=pending. Returns the new row id."""
        row = {
            "brand":    brand,
            "renderer": renderer,
            "template": template,
            "content":  content,
            "format":   format,
            "status":   "pending",
        }
        resp = self._client.table("content_jobs").insert(row).execute()
        row_id = resp.data[0]["id"] if resp.data else ""
        log.info("content_job_queued id=%s brand=%s template=%s", row_id, brand, template)
        return row_id
