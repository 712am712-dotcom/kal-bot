"""
fallback_notifier.py — Fallback communication system for Kal.

Priority order for every message:
  1. Discord (primary — always try first when status is UP)
  2. Email   (fallback — if Discord fails, send to KAL_OWNER_EMAIL via Gmail SMTP)
  3. Supabase (always — log every message to bot_communications table)
  4. Local file (always — bot/kal_communications.log)

Discord health tracking:
  - 3 consecutive failures → discord_status = DOWN
  - While DOWN: skip Discord entirely, route straight to email
  - Every 30 minutes while DOWN: one recovery probe
  - On recovery: post catch-up summary to #alerts, clear missed-message buffer

Usage (called by discord_notifier._send):
  await route_send(channel, payload, discord_send_fn=_discord_raw, message_type="trade")
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

# ── Local log file ─────────────────────────────────────────────────────────────
_LOG_PATH = Path(__file__).parent / "kal_communications.log"

# ── Discord health state ───────────────────────────────────────────────────────
_discord_status:       str   = "UP"   # "UP" | "DOWN"
_consecutive_failures: int   = 0
_FAILURE_THRESHOLD:    int   = 3
_RECOVERY_INTERVAL:    float = 1800.0  # 30 minutes
_last_recovery_check:  float = 0.0

# Buffer of messages missed while Discord was DOWN (for catch-up summary)
_missed_messages: list[dict] = []


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _now_label() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


# ── Payload → readable text ───────────────────────────────────────────────────

def _extract_text(payload: dict) -> str:
    """Pull human-readable text from a Discord payload for logging and email."""
    if payload.get("content"):
        return str(payload["content"])
    embeds = payload.get("embeds", [])
    if embeds:
        e = embeds[0]
        parts: list[str] = []
        if e.get("title"):
            parts.append(e["title"])
        if e.get("description"):
            parts.append(e["description"])
        for f in e.get("fields", []):
            name  = f.get("name", "")
            value = f.get("value", "")
            if name or value:
                parts.append(f"{name}: {value}" if name else value)
        return "\n".join(parts)
    return str(payload)[:500]


# ── Local file logging ─────────────────────────────────────────────────────────

def _log_to_file(
    channel:         str,
    message_type:    str,
    content:         str,
    delivery_method: str,
    status:          str,
) -> None:
    try:
        record = json.dumps({
            "timestamp":       _ts(),
            "channel":         channel,
            "type":            message_type or channel,
            "delivery_method": delivery_method,
            "status":          status,
            "content":         content[:300],
        })
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception as exc:
        log.debug("[fallback] file log error: %s", exc)


# ── Supabase logging ──────────────────────────────────────────────────────────

async def _log_to_supabase(
    channel:         str,
    message_type:    str,
    content:         str,
    delivery_method: str,
    status:          str,
) -> None:
    try:
        from config import settings
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return
        url = f"{settings.supabase_url}/rest/v1/bot_communications"
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        }
        body = {
            "channel":         channel,
            "message_type":    message_type or channel,
            "content":         content[:2000],
            "delivery_method": delivery_method,
            "status":          status,
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers=headers, json=body)
            if r.status_code not in (200, 201):
                log.debug("[fallback] supabase log %s: %s", r.status_code, r.text[:120])
    except Exception as exc:
        log.debug("[fallback] supabase log error: %s", exc)


# ── Email fallback ─────────────────────────────────────────────────────────────

def _send_email_sync(channel: str, content: str) -> bool:
    """Synchronous Gmail SMTP send. Returns True on success."""
    try:
        from config import settings
        from_addr = settings.kal_email_address
        password  = settings.kal_email_password
        to_addr   = getattr(settings, "kal_owner_email", "")

        if not from_addr or not password or not to_addr:
            log.warning(
                "[fallback] email fallback skipped — missing KAL_EMAIL_ADDRESS, "
                "KAL_EMAIL_PASSWORD, or KAL_OWNER_EMAIL"
            )
            return False

        # Subject: first non-empty line of content, capped at 60 chars
        first_line = next((l.strip() for l in content.splitlines() if l.strip()), "notification")
        # Strip Discord markdown bold markers for the subject
        first_line = first_line.replace("**", "").strip()[:60]
        subject = f"Kal Alert — #{channel} — {first_line}"

        body = (
            f"{content}\n\n"
            f"---\n"
            f"Sent via email fallback — Discord was unavailable at {_now_label()}"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_addr
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, to_addr, msg.as_string())

        log.info("[fallback] email sent to %s (channel #%s)", to_addr, channel)
        return True

    except Exception as exc:
        log.warning("[fallback] email send failed: %s", exc)
        return False


async def _send_email(channel: str, content: str) -> bool:
    """Async wrapper — runs Gmail SMTP in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _send_email_sync, channel, content)


# ── Discord health tracking ───────────────────────────────────────────────────

def _mark_discord_success() -> None:
    global _consecutive_failures, _discord_status
    _consecutive_failures = 0
    _discord_status = "UP"


def _mark_discord_failure() -> None:
    global _consecutive_failures, _discord_status
    _consecutive_failures += 1
    if _consecutive_failures >= _FAILURE_THRESHOLD and _discord_status != "DOWN":
        _discord_status = "DOWN"
        log.warning(
            "[fallback] Discord marked DOWN after %d consecutive failures — "
            "routing to email fallback",
            _consecutive_failures,
        )


def _should_attempt_recovery() -> bool:
    """Return True once per _RECOVERY_INTERVAL while Discord is DOWN."""
    global _last_recovery_check
    if _discord_status != "DOWN":
        return False
    now = time.monotonic()
    if now - _last_recovery_check >= _RECOVERY_INTERVAL:
        _last_recovery_check = now
        return True
    return False


async def _post_recovery_summary(discord_send_fn: Callable, missed_count: int) -> None:
    """Post a catch-up summary to #alerts after Discord recovers."""
    summary = (
        f"**Discord Reconnected**\n"
        f"Kal's connection to Discord was restored. "
        f"{missed_count} message(s) were sent via email fallback during the outage.\n"
        f"All messages were also logged to Supabase and `kal_communications.log`."
    )
    try:
        await discord_send_fn("alerts", {
            "embeds": [{
                "description": summary,
                "color": 0x00C076,
                "timestamp": _ts().rstrip("Z"),
            }],
            "username": "Kal",
        })
    except Exception as exc:
        log.warning("[fallback] recovery summary post failed: %s", exc)


# ── Core router ──────────────────────────────────────────────────────────────

async def route_send(
    channel:         str,
    payload:         dict,
    *,
    discord_send_fn: Callable[[str, dict], Awaitable[None]],
    message_type:    str = "",
) -> None:
    """
    Send a message through the full fallback stack. Never raises.

    Args:
        channel:         Discord channel name (e.g. "alerts", "trades")
        payload:         Discord message payload dict
        discord_send_fn: Async callable(channel, payload) that does the raw Discord send
        message_type:    Optional label for Supabase logging (e.g. "trade_placed")
    """
    global _missed_messages

    content    = _extract_text(payload)
    discord_ok = False
    email_ok   = False
    recovered  = False

    # ── 1. Discord ─────────────────────────────────────────────────────────────
    if _discord_status == "UP" or _should_attempt_recovery():
        was_down = _discord_status == "DOWN"
        try:
            await discord_send_fn(channel, payload)
            _mark_discord_success()
            discord_ok = True

            if was_down and _missed_messages:
                missed_count = len(_missed_messages)
                _missed_messages = []
                recovered = True
                await _post_recovery_summary(discord_send_fn, missed_count)

        except Exception as exc:
            _mark_discord_failure()
            log.warning("[fallback] Discord send failed (#%s): %s", channel, exc)

    # ── 2. Email fallback ──────────────────────────────────────────────────────
    if not discord_ok:
        _missed_messages.append({
            "channel": channel,
            "ts":      _ts(),
            "preview": content[:100],
        })
        email_ok = await _send_email(channel, content)

    # ── 3 & 4. Always log ─────────────────────────────────────────────────────
    if discord_ok:
        delivery = "discord"
        status   = "delivered"
    elif email_ok:
        delivery = "email"
        status   = "fallback_used"
    else:
        delivery = "log_only"
        status   = "failed"

    _log_to_file(channel, message_type, content, delivery, status)
    await _log_to_supabase(channel, message_type, content, delivery, status)


async def route_send_file(
    channel:         str,
    payload:         dict,
    *,
    discord_send_fn: Callable[[str, dict, str, bytes, str], Awaitable[None]],
    filename:        str,
    file_bytes:      bytes,
    mime:            str,
    message_type:    str = "",
) -> None:
    """
    Route a file-attachment send. No email fallback for files (too large).
    Logs the fact of the send (success or failure) to Supabase and local file.
    Never raises.
    """
    content = _extract_text(payload)
    try:
        await discord_send_fn(channel, payload, filename, file_bytes, mime)
        delivery = "discord"
        status   = "delivered"
    except Exception as exc:
        log.warning("[fallback] file send failed (#%s, %s): %s", channel, filename, exc)
        delivery = "log_only"
        status   = "failed"

    _log_to_file(channel, message_type, content, delivery, status)
    await _log_to_supabase(channel, message_type, content, delivery, status)


def discord_is_up() -> bool:
    """Public accessor for other modules that want to know Discord health."""
    return _discord_status == "UP"
