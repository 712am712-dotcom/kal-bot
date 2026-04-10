"""
newsletter_intel.py — Tier-1 newsletter intelligence pipeline.

Tier-1 senders are high-signal research sources treated as immediate intelligence
signals, not background reading. When a new email arrives from a tier-1 sender:
  1. Fetched via IMAP (looks back 2 days to avoid missing yesterday's emails)
  2. Claude evaluates: thesis, named trade ideas, tickers, signal score 1–10
  3. Structured signal posted to #intelligence-feed
  4. Saved to Supabase bot_communications (message_type=newsletter_intel)

Conflict/confirm check:
  build_conflict_note(trade_today_text, recent_intel) does a keyword sweep of
  recent tier-1 intel against The Trade Today asset names and returns a plain
  English note appended to the Trade Today post — no extra Claude call needed.

Called from main.py:
  - _newsletter_intel_task(): every 30 min, EmailReader instance passed in
  - build_conflict_note(): called inside _gmail_brief_task before caching Trade Today

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

# ── Asset keyword map — used for conflict detection (no Claude call) ───────────
# Map common asset names / tickers to canonical labels.
_ASSET_ALIASES: dict[str, str] = {
    # Oil / energy
    "oil":    "oil",  "crude":  "oil",  "wti":   "oil",  "brent": "oil",
    "cl":     "oil",  "clz":   "oil",   "energy": "oil",  "opec":  "oil",
    "lng":    "oil",  "gas":   "oil",
    # Gold / metals
    "gold":   "gold", "gld":  "gold",   "silver": "silver", "copper": "copper",
    # Equities
    "spx":    "SPX",  "spy":  "SPX",    "nasdaq": "NASDAQ", "qqq": "NASDAQ",
    "dow":    "DOW",  "djia": "DOW",
    # Rates / bonds
    "yield":  "rates", "treasury": "rates", "10y": "rates", "2y": "rates",
    "fed":    "rates", "rate":     "rates",
    # Crypto
    "bitcoin": "BTC", "btc":      "BTC",
    "ethereum": "ETH", "eth":     "ETH",
    "solana":  "SOL", "sol":      "SOL",
    "crypto":  "crypto",
    # Macro
    "dollar":  "USD",  "dxy":    "USD",  "yen":   "JPY",  "euro": "EUR",
    "china":   "China", "japan": "Japan", "iran":  "Iran",
    "hormuz":  "oil",   "strait": "oil",  "opec":  "oil",
}


def _extract_assets(text: str) -> set[str]:
    """Return the set of canonical asset labels mentioned in text."""
    words = re.findall(r"\b[A-Za-z0-9]+\b", text.lower())
    found: set[str] = set()
    for w in words:
        label = _ASSET_ALIASES.get(w)
        if label:
            found.add(label)
    return found


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


# ── Supabase recent intel query ────────────────────────────────────────────────

async def get_recent_tier1_intel(hours: int = 24) -> list[dict]:
    """
    Query Supabase bot_communications for recent newsletter_intel entries.
    Returns list of {content, created_at} for the last `hours` hours.
    Used by build_conflict_note() for cross-referencing Trade Today.
    """
    try:
        from config import settings
        if not settings.supabase_url or not settings.supabase_service_role_key:
            return []

        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        ).isoformat() + "Z"

        url = (
            f"{settings.supabase_url}/rest/v1/bot_communications"
            f"?message_type=eq.newsletter_intel"
            f"&created_at=gte.{cutoff}"
            f"&order=created_at.desc"
            f"&limit=20"
        )
        headers = {
            "apikey":        settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url, headers=headers)
        if r.status_code == 200:
            return r.json()
        log.debug("[tier1] supabase query %s: %s", r.status_code, r.text[:100])
    except Exception as exc:
        log.debug("[tier1] supabase query failed: %s", exc)
    return []


# ── Conflict / confirm note ────────────────────────────────────────────────────

def build_conflict_note(trade_today_text: str, recent_intel: list[dict]) -> str:
    """
    Compare The Trade Today text against recent tier-1 intel entries.
    Returns a plain-English note if any asset overlap is found, else "".

    Pure Python — no Claude call. Uses keyword matching via _ASSET_ALIASES.
    The note is appended to the Trade Today Discord post.

    Example output:
      "**Newsletter cross-check:** Citrini Research (Apr 8) also covered oil —
       directional alignment on crude. Check #intelligence-feed for full thesis."
    """
    if not recent_intel:
        return ""

    trade_assets = _extract_assets(trade_today_text)
    if not trade_assets:
        return ""

    matches: list[str] = []
    for entry in recent_intel:
        content = entry.get("content", "")
        intel_assets = _extract_assets(content)
        overlap = trade_assets & intel_assets
        if not overlap:
            continue

        # Extract source name from stored content (first line of formatted post)
        source_match = re.search(r"📡\s*\*\*(.+?)\*\*", content)
        source = source_match.group(1) if source_match else "Tier-1 research"

        # Extract date from created_at
        created = entry.get("created_at", "")
        try:
            dt  = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            day = dt.strftime("%b %-d")
        except Exception:
            day = "recent"

        asset_str = " · ".join(sorted(overlap))
        matches.append(f"{source} ({day}) — overlap on {asset_str}")

    if not matches:
        return ""

    bullet_str = "\n".join(f"• {m}" for m in matches[:3])
    return (
        f"\n\n**Newsletter cross-check** — recent tier-1 research covers same assets:\n"
        f"{bullet_str}\n"
        f"_See #intelligence-feed for full thesis and trade ideas._"
    )


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
