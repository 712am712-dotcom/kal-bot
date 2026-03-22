"""
gmail_reader.py — Reads Kal's daily financial newsletter from Gmail.

Uses Gmail API (read-only OAuth2) to find today's newsletter email from a
configured sender, extracts the body text, and calls Claude once to produce
a "what's the trade?" morning brief for Discord.

OAuth2 setup (one-time):
  1. Go to console.cloud.google.com → New project → Enable Gmail API
  2. Create OAuth2 credentials (Desktop app) → Download as gmail_credentials.json
  3. Place gmail_credentials.json in the bot/ folder
  4. First run: bot opens browser for one-time authorization
  5. Token saved to gmail_token.json — subsequent runs are fully automatic

Never sends, deletes, or modifies any email. Read-only scope only.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import html as html_module
import logging
import os
import re
from functools import partial
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Directory that contains gmail_reader.py — used to resolve relative paths
# so the bot works regardless of which directory Python is invoked from.
_BOT_DIR = Path(__file__).parent


def _resolve_path(p: str) -> Path:
    """
    Resolve a path relative to the bot/ directory.
    Absolute paths are returned as-is.
    """
    path = Path(p)
    if path.is_absolute():
        return path
    # Strip leading ./ if present, then anchor to bot dir
    return _BOT_DIR / path.name


# ── OAuth2 / Service ──────────────────────────────────────────────────────────

def _build_gmail_service(credentials_path: str, token_path: str) -> Any:
    """
    Synchronous — loads or creates OAuth2 credentials and returns a Gmail service.
    Opened in a thread executor so it doesn't block the event loop.
    On first run (no token), opens a browser for one-time authorization.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Gmail packages not installed. Run: "
            "pip install google-auth google-auth-oauthlib google-api-python-client"
        ) from exc

    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            log.info("[gmail] token refreshed")
        else:
            if not Path(credentials_path).exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {credentials_path}. "
                    "See bot/README.md for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            log.info("[gmail] OAuth2 authorization complete")

        Path(token_path).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Email body extraction ─────────────────────────────────────────────────────

def _strip_html(raw_html: str) -> str:
    """Strip HTML tags and decode entities, preserving readable structure."""
    # Remove script / style blocks entirely
    raw_html = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r"<style[^>]*>.*?</style>",  " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Block elements → newlines
    raw_html = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", raw_html, flags=re.IGNORECASE)
    # Strip all remaining tags
    raw_html = re.sub(r"<[^>]+>", " ", raw_html)
    # Decode HTML entities
    text = html_module.unescape(raw_html)
    # Collapse whitespace
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _decode_body_data(data: str) -> str:
    """Decode base64url-encoded Gmail body data."""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_from_payload(payload: dict) -> str:
    """
    Recursively walk a Gmail MIME payload tree.
    Prefers text/plain; falls back to text/html (stripped).
    """
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return _decode_body_data(body_data)

    if mime == "text/html" and body_data:
        return _strip_html(_decode_body_data(body_data))

    # Multipart — prefer plain parts first
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _extract_body_from_payload(part)
            if text:
                return text

    # No plain text found — try anything
    for part in parts:
        text = _extract_body_from_payload(part)
        if text:
            return text

    return ""


# ── Gmail search ──────────────────────────────────────────────────────────────

def _find_todays_newsletter_sync(service: Any, sender_email: str) -> str | None:
    """
    Synchronous — search Gmail for today's email from sender_email.
    Returns the body text, or None if not found.
    """
    today = datetime.date.today()
    # Gmail search: from:addr AND after date (YYYY/MM/DD)
    query = f"from:{sender_email} after:{today.strftime('%Y/%m/%d')}"
    result = service.users().messages().list(
        userId="me", q=query, maxResults=3
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        log.debug("[gmail] no newsletter found today from %s", sender_email)
        return None

    # Get the most recent message
    msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="full"
    ).execute()

    body = _extract_body_from_payload(msg.get("payload", {}))
    if not body:
        log.warning("[gmail] newsletter found but body is empty")
        return None

    subject = ""
    for header in msg.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "subject":
            subject = header.get("value", "")
            break

    log.info("[gmail] newsletter found: %s (%d chars)", subject[:60], len(body))
    return body


# ── Claude brief builder ──────────────────────────────────────────────────────

BRIEF_SYSTEM = """\
You are Kal, an expert prediction market and financial analyst.
You read morning financial newsletters and distill them into fast, actionable
trading intelligence. Every point must have a market implication — if it
doesn't affect markets, skip it. Write like a trader briefing another trader.
Fast, direct, no fluff. Under 15 words per bullet.
"""

BRIEF_PROMPT = """\
Today is {date}. Read this financial newsletter and produce a morning brief.

NEWSLETTER TEXT:
{newsletter}

TOP KALSHI MARKETS RIGHT NOW (for the Prediction Market Angle section):
{kalshi_block}

Produce EXACTLY this format — every section is mandatory:

**Morning Brief — {date}**
*What matters today. What's the trade.*

**MACRO**
- [Item] — [market implication in under 15 words]
- [Item] — [market implication in under 15 words]
(2-4 bullets, only items that move markets)

**CRYPTO**
- [Item] — [1-line implication for BTC/ETH/SOL]
(1-3 bullets, crypto-specific news only)

**EARNINGS** (omit section entirely if no earnings in newsletter)
- [Company] beat/missed — [one word reason] ✅/❌

**BIG MONEY MOVING**
- [Notable deal/funding/move] — why it matters for markets
(1-3 bullets, significant capital flows only)

**TRADER'S ANGLE**
- Stocks: [Key movers, sectors showing strength/weakness]
- Crypto: [BTC/ETH/SOL overnight action, sentiment, key levels if mentioned]
- Commodities: [Oil, gold, silver — significant moves only]
- Rates: [Bond market, yield curve, Fed implications if mentioned]
- Sectors: [Which sectors leading/lagging and why]
- Setup of the day: [One specific trade setup worth watching today]

**PREDICTION MARKET ANGLE**
[2-3 sentences connecting today's headlines to Kalshi prediction markets. Be specific — name actual markets from the list above and their current prices. Explain why they might be mispriced given today's news.]

**TODAY'S FOCUS**
[1-2 sentences — the single most important thing to watch today and why it matters for prediction markets specifically]

Rules:
- Every bullet must have a direct market implication — skip anything else
- Prediction market angle is MANDATORY — always connect news to Kalshi
- Keep each bullet under 15 words
- TODAY'S FOCUS must be prediction-market oriented
- If a section genuinely has no content from the newsletter, write "— nothing notable today"
"""


async def build_morning_brief(
    newsletter_text: str,
    kalshi_markets: list[dict],
    model_override: str | None = None,
) -> str:
    """
    Build the morning brief from newsletter text (ONE Claude call).
    Returns formatted Discord message content.
    """
    from config import settings
    import anthropic

    date_str = datetime.datetime.now().strftime("%A, %B %-d")

    # Top Kalshi markets for the prediction market angle
    sorted_m = sorted(
        kalshi_markets,
        key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
        reverse=True,
    )[:8]
    kalshi_block = "\n".join(
        f"• {(m.get('title') or '?')[:65]} — "
        f"{round(float(m.get('yes_ask_dollars', 0.5) or 0.5) * 100)}%"
        for m in sorted_m
    ) or "No markets loaded"

    # Truncate very long newsletters (Claude context limit)
    newsletter_trimmed = newsletter_text[:8000]
    if len(newsletter_text) > 8000:
        newsletter_trimmed += "\n... [truncated]"

    prompt = BRIEF_PROMPT.format(
        date=date_str,
        newsletter=newsletter_trimmed,
        kalshi_block=kalshi_block,
    )

    active_model = model_override or settings.claude_model

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=active_model,
        max_tokens=1200,
        system=BRIEF_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    brief = message.content[0].text.strip()
    in_tok  = message.usage.input_tokens
    out_tok = message.usage.output_tokens
    if "haiku" in active_model.lower():
        cost = in_tok * 0.80 / 1_000_000 + out_tok * 4.0 / 1_000_000
    else:
        cost = in_tok * 15.0 / 1_000_000 + out_tok * 75.0 / 1_000_000

    log.info("[gmail] brief built: %d chars, cost=$%.4f, model=%s", len(brief), cost, active_model)
    return brief, round(cost, 6)


# ── GmailReader orchestrator ──────────────────────────────────────────────────

class GmailReader:
    """
    Fetches the morning newsletter and builds the morning brief.
    Tracks whether the brief has been posted today to avoid duplicates.
    """

    def __init__(self, credentials_path: str, token_path: str) -> None:
        self._creds_path = str(_resolve_path(credentials_path))
        self._token_path = str(_resolve_path(token_path))
        self._service: Any = None
        self._posted_date: str = ""

    @property
    def is_configured(self) -> bool:
        return Path(self._creds_path).exists()

    async def _get_service(self) -> Any:
        """Async wrapper — builds Gmail service in thread pool."""
        if self._service is not None:
            return self._service
        loop = asyncio.get_event_loop()
        svc = await loop.run_in_executor(
            None,
            partial(_build_gmail_service, self._creds_path, self._token_path),
        )
        self._service = svc
        return svc

    async def fetch_newsletter(self, sender_email: str) -> str | None:
        """
        Async — search Gmail for today's newsletter from sender_email.
        Returns body text or None.
        """
        if not self.is_configured:
            log.debug("[gmail] credentials not found at %s — skipping", self._creds_path)
            return None
        try:
            svc  = await self._get_service()
            loop = asyncio.get_event_loop()
            body = await loop.run_in_executor(
                None,
                partial(_find_todays_newsletter_sync, svc, sender_email),
            )
            return body
        except FileNotFoundError as exc:
            log.warning("[gmail] %s", exc)
            return None
        except Exception as exc:
            log.warning("[gmail] fetch_newsletter failed: %s", exc)
            # Reset service so next attempt retries auth
            self._service = None
            return None

    def already_posted_today(self) -> bool:
        return self._posted_date == datetime.date.today().isoformat()

    def mark_posted(self) -> None:
        self._posted_date = datetime.date.today().isoformat()


# ── Convenience export ────────────────────────────────────────────────────────

def extract_todays_focus(brief: str) -> str:
    """Pull just the TODAY'S FOCUS section from a brief for #intelligence."""
    match = re.search(
        r"\*\*TODAY'S FOCUS\*\*(.*?)(?:\*\*[A-Z]|\Z)",
        brief,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        return "**Today's Focus**\n" + match.group(1).strip()
    return ""
