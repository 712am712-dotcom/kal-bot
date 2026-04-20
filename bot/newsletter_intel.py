"""
newsletter_intel.py — Tier-1 newsletter intelligence pipeline.

Tier-1 senders are high-signal research sources treated as immediate intelligence
signals, not background reading. When a new email arrives from a tier-1 sender:
  1. Fetched via IMAP (looks back 2 days to avoid missing yesterday's emails)
  2. Claude evaluates: thesis, named trade ideas, tickers, signal score 1–10
  3. Structured signal posted to #intelligence-feed
  4. Saved to Supabase bot_communications (message_type=newsletter_intel)

Called from main.py:
  - _newsletter_intel_task(): every 30 min, EmailReader instance passed in

UID dedup resets at midnight each day. Max MAX_INTEL_PER_DAY posts/day total.
"""
from __future__ import annotations

import asyncio
import datetime
import email as email_mod
import imaplib
import json
import logging
import re
import time
from functools import partial
from typing import Any

import httpx

from claude_client import KAL_IDENTITY
from email_reader import _imap_server_for, _parse_body_from_message

log = logging.getLogger(__name__)

# ── Tier-1 sender registry ─────────────────────────────────────────────────────
# Each entry: {email, name, focus}
# "focus" is shown in the Discord post header so readers know the source's lens.
TIER1_SENDERS: list[dict] = [
    {
        "email": "citrini@substack.com",
        "name":  "Citrini Research",
        "focus": "macro · commodities · geopolitical trade ideas",
    },
    # Add more tier-1 senders here as identified:
    # {"email": "author@substack.com", "name": "Source Name", "focus": "focus area"},
]

# ── Daily limits ───────────────────────────────────────────────────────────────
MAX_INTEL_PER_DAY = 10  # max #intelligence-feed posts from tier-1 newsletters/day

# ── Module-level dedup state ───────────────────────────────────────────────────
_processed_uids:  set[str] = set()
_processed_date:  str      = ""
_intel_count:     int      = 0
_intel_date:      str      = ""

# ── IMAP fetch for tier-1 senders ─────────────────────────────────────────────

def _imap_fetch_tier1_sync(
    email_address: str,
    password: str,
    senders: list[dict],       # list of TIER1_SENDERS entries
    processed_uids: frozenset[str],
    since_days: int = 2,       # look back this many days to catch yesterday's email
) -> list[dict]:
    """
    Open ONE IMAP connection and fetch all unprocessed emails from tier-1 senders.
    Returns list of {uid, subject, from_name, email, body} for new emails only.

    since_days=2 means we search SINCE yesterday — catches emails that arrived
    after the last scan or before the scanner was first deployed.
    """
    host, port = _imap_server_for(email_address)
    since_date = datetime.date.today() - datetime.timedelta(days=since_days - 1)
    since_str  = since_date.strftime("%d-%b-%Y")
    results: list[dict] = []

    try:
        with imaplib.IMAP4_SSL(host, port) as mail:
            mail.login(email_address, password)
            mail.select("INBOX")

            for sender in senders:
                sender_email = sender["email"]
                search_term  = sender_email.lstrip("@")
                try:
                    _, data = mail.search(None, f'FROM "{search_term}" SINCE "{since_str}"')
                    msg_ids = data[0].split()
                    if not msg_ids:
                        log.debug("[tier1] no emails from %s since %s", sender_email, since_str)
                        continue

                    for msg_id in msg_ids:
                        try:
                            # Get UID
                            _, uid_data = mail.fetch(msg_id, "(UID)")
                            uid_raw = uid_data[0]
                            uid_str = uid_raw.decode() if isinstance(uid_raw, bytes) else str(uid_raw)
                            uid_match = re.search(r"UID\s+(\d+)", uid_str)
                            uid = uid_match.group(1) if uid_match else msg_id.decode()

                            if uid in processed_uids:
                                log.debug("[tier1] skipping already-processed uid=%s", uid)
                                continue

                            _, msg_data = mail.fetch(msg_id, "(RFC822)")
                            raw = msg_data[0][1]
                            msg = email_mod.message_from_bytes(raw)

                            # Decode subject
                            from email.header import decode_header as _dh
                            subj_raw = _dh(msg.get("Subject", ""))[0]
                            subject = (
                                subj_raw[0].decode(subj_raw[1] or "utf-8")
                                if isinstance(subj_raw[0], bytes)
                                else (subj_raw[0] or "")
                            )

                            body = _parse_body_from_message(msg)
                            if not body:
                                log.debug("[tier1] empty body uid=%s", uid)
                                continue

                            log.info(
                                "[tier1] new email from %s: %s (uid=%s, %d chars)",
                                sender["name"], subject[:60], uid, len(body),
                            )
                            results.append({
                                "uid":       uid,
                                "subject":   subject,
                                "from_name": sender["name"],
                                "email":     sender_email,
                                "focus":     sender.get("focus", ""),
                                "body":      body,
                            })

                        except Exception as exc:
                            log.warning("[tier1] error processing uid from %s: %s", sender_email, exc)
                            continue

                except Exception as exc:
                    log.warning("[tier1] IMAP search failed for %s: %s", sender_email, exc)
                    continue

    except imaplib.IMAP4.error as exc:
        log.warning("[tier1] IMAP auth/connection error: %s", exc)
    except Exception as exc:
        log.warning("[tier1] connection failed: %s", exc)

    return results


# ── Claude evaluation ──────────────────────────────────────────────────────────

_EVAL_SYSTEM = KAL_IDENTITY + """
You are evaluating a tier-1 research newsletter for market intelligence.
Extract the structured signal from the email and respond ONLY with valid JSON.
No preamble. No markdown fences. Valid JSON only.
"""

_EVAL_PROMPT = """\
Tier-1 research email received. Extract the market intelligence.

Source: {source_name}
Subject: {subject}
Body (first 2000 chars):
{body}

Respond with ONLY this JSON (no markdown, no preamble):
{{
  "thesis": "2-3 sentence core argument or thesis from the email",
  "trade_ideas": ["specific trade 1", "specific trade 2"],
  "tickers": ["ASSET1", "ASSET2"],
  "signal_score": 7,
  "urgency": "now" | "today" | "week",
  "one_liner": "≤15-word Discord summary — what's the call and why now"
}}

Rules:
- thesis: the author's actual argument, not a summary of the email
- trade_ideas: name the specific instrument (e.g. "long CLZ6 short front month", "short EUR/USD")
  Include EVERY specific trade idea the author names. Empty list [] if none named.
- tickers: every asset, ticker, commodity explicitly mentioned (e.g. ["CLZ6", "WTI", "EUR"])
- signal_score: 1=noise, 5=interesting, 8=high conviction, 10=rare must-act signal
- urgency: "now" = act today, "today" = this week, "week" = next 1-4 weeks
- one_liner: punchy header for the Discord post, names the asset and direction
"""


async def evaluate_tier1_email(
    email_data: dict,
    api_key: str,
    model: str,
) -> tuple[dict | None, float]:
    """
    Evaluate a tier-1 email via Claude. Returns (result_dict, cost) or (None, cost).
    result_dict keys: thesis, trade_ideas, tickers, signal_score, urgency, one_liner
    """
    import anthropic as _anthropic

    prompt = _EVAL_PROMPT.format(
        source_name=email_data["from_name"],
        subject=email_data["subject"][:200],
        body=email_data["body"][:2000],
    )

    t0   = time.time()
    cost = 0.0
    try:
        client   = _anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=_EVAL_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        inp = response.usage.input_tokens
        out = response.usage.output_tokens

        # Pricing: Sonnet or Opus rates depending on model
        if "haiku" in model.lower():
            cost = inp * 0.80 / 1_000_000 + out * 4.0 / 1_000_000
        elif "sonnet" in model.lower():
            cost = inp * 3.0 / 1_000_000 + out * 15.0 / 1_000_000
        else:
            cost = inp * 15.0 / 1_000_000 + out * 75.0 / 1_000_000

        log.info(
            "[tier1] eval %s tokens=%d+%d cost=$%.4f ms=%d",
            email_data["from_name"], inp, out, cost, round((time.time() - t0) * 1000),
        )

        # Strip any accidental markdown fences
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())

        result = json.loads(raw)
        return result, cost

    except json.JSONDecodeError as exc:
        log.warning("[tier1] JSON parse failed for %s: %s | raw=%s",
                    email_data["from_name"], exc, raw[:200] if "raw" in dir() else "")
        return None, cost
    except Exception as exc:
        log.warning("[tier1] Claude call failed for %s: %s", email_data["from_name"], exc)
        return None, cost


# ── Main scanner class ─────────────────────────────────────────────────────────

class NewsletterIntelScanner:
    """
    Stateful scanner that fetches and evaluates tier-1 newsletter emails.
    Instantiate once at startup; call scan() every 30 minutes.
    """

    def __init__(self, imap_address: str, imap_password: str) -> None:
        self._imap_address  = imap_address.strip()
        self._imap_password = imap_password.strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._imap_address and self._imap_password)

    async def scan(
        self,
        api_key: str,
        model: str,
        post_fn: Any,   # async callable(email_data, result) → None
    ) -> int:
        """
        Fetch new tier-1 emails, evaluate each, call post_fn for each hit.
        Returns number of emails evaluated (not necessarily posted).

        Resets UID tracking + daily count at midnight.
        Skips entirely if daily cap reached.
        """
        global _processed_uids, _processed_date, _intel_count, _intel_date

        if not self.is_configured or not TIER1_SENDERS:
            return 0

        today = datetime.date.today().isoformat()

        # Reset at midnight
        if today != _processed_date:
            _processed_uids  = set()
            _processed_date  = today
        if today != _intel_date:
            _intel_count = 0
            _intel_date  = today

        if _intel_count >= MAX_INTEL_PER_DAY:
            log.debug("[tier1] daily cap (%d) reached, skipping scan", MAX_INTEL_PER_DAY)
            return 0

        # Fetch — run sync IMAP in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        try:
            emails = await loop.run_in_executor(
                None,
                partial(
                    _imap_fetch_tier1_sync,
                    self._imap_address,
                    self._imap_password,
                    TIER1_SENDERS,
                    frozenset(_processed_uids),
                    2,  # since_days: look back 2 days
                ),
            )
        except Exception as exc:
            log.warning("[tier1] IMAP scan failed: %s", exc)
            return 0

        if not emails:
            return 0

        log.info("[tier1] %d new tier-1 email(s) to evaluate", len(emails))
        evaluated = 0

        for email_data in emails:
            # Mark as processed immediately so a crash doesn't cause a re-post
            _processed_uids.add(email_data["uid"])

            if _intel_count >= MAX_INTEL_PER_DAY:
                log.info("[tier1] daily cap hit mid-batch, stopping")
                break

            try:
                result, cost = await evaluate_tier1_email(email_data, api_key, model)
                evaluated += 1
                if cost > 0:
                    log.info("[tier1] eval cost=$%.4f", cost)

                if result is None:
                    log.warning("[tier1] eval returned None for %s", email_data["from_name"])
                    continue

                await post_fn(email_data, result)
                _intel_count += 1
                log.info("[tier1] posted intel #%d today from %s", _intel_count, email_data["from_name"])

            except Exception as exc:
                log.warning("[tier1] post failed for %s: %s", email_data["from_name"], exc)

        return evaluated
