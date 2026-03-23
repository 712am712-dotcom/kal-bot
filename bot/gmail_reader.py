"""
gmail_reader.py -- Reads Kal's daily financial newsletter from Gmail.

Two authentication methods -- auto-detected from env vars:

  IMAP + App Password (Railway / 24x7):
    Set KAL_GMAIL_ADDRESS and KAL_GMAIL_APP_PASSWORD in Railway Variables.
    No browser required. Works headless forever.
    Setup: myaccount.google.com -> Security -> App Passwords -> Mail

  OAuth2 (local development):
    Set GMAIL_CREDENTIALS_PATH and GMAIL_TOKEN_PATH.
    First run opens browser for one-time authorization.
    Requires google-auth / google-api-python-client packages.

If KAL_GMAIL_ADDRESS + KAL_GMAIL_APP_PASSWORD are both set, IMAP is used.
Otherwise falls back to OAuth2 (for local use).

Never sends, deletes, or modifies any email. Read-only.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import email as email_mod
import html as html_module
import imaplib
import logging
import re
from email.header import decode_header as _decode_header
from functools import partial
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES  = ["https://www.googleapis.com/auth/gmail.readonly"]
_BOT_DIR = Path(__file__).parent


def _resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return _BOT_DIR / path.name


# ---- HTML / body helpers -----------------------------------------------------

def _strip_html(raw_html: str) -> str:
    """Strip HTML tags and decode entities, preserving readable structure."""
    raw_html = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r"<style[^>]*>.*?</style>",   " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r"<br\s*/?>",                 "\n", raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r"</(p|div|li|tr|h[1-6])>",  "\n", raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r"<[^>]+>", " ", raw_html)
    text  = html_module.unescape(raw_html)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _decode_body_data(data: str) -> str:
    """Decode base64url-encoded Gmail API body data."""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_from_payload(payload: dict) -> str:
    """Recursively walk a Gmail API MIME payload. Prefers text/plain."""
    mime      = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return _decode_body_data(body_data)
    if mime == "text/html" and body_data:
        return _strip_html(_decode_body_data(body_data))

    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _extract_body_from_payload(part)
            if text:
                return text
    for part in parts:
        text = _extract_body_from_payload(part)
        if text:
            return text
    return ""


# ---- IMAP / App Password method (Railway) ------------------------------------

def _imap_fetch_sync(gmail_address: str, app_password: str, sender_email: str) -> str | None:
    """
    Synchronous IMAP fetch -- run in a thread executor.
    Connects to Gmail IMAP with the App Password and searches for today's
    email from sender_email. Returns the plain-text body or None.
    """
    today = datetime.date.today()
    since_str = today.strftime("%d-%b-%Y")   # IMAP format: 23-Mar-2026

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as mail:
            mail.login(gmail_address, app_password)
            mail.select("INBOX")

            # Search by sender + date
            _, data = mail.search(None, f'FROM "{sender_email}" SINCE "{since_str}"')
            msg_ids = data[0].split()
            if not msg_ids:
                log.debug("[gmail-imap] no newsletter from %s since %s", sender_email, since_str)
                return None

            # Fetch the most recent match (last ID = newest in IMAP)
            _, msg_data = mail.fetch(msg_ids[-1], "(RFC822)")
            raw = msg_data[0][1]

        msg = email_mod.message_from_bytes(raw)

        # Decode subject for logging
        subj_raw = _decode_header(msg.get("Subject", ""))[0]
        subject  = (
            subj_raw[0].decode(subj_raw[1] or "utf-8")
            if isinstance(subj_raw[0], bytes)
            else (subj_raw[0] or "")
        )

        # Extract body -- prefer text/plain, fall back to text/html
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct      = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(part.get_content_charset("utf-8") or "utf-8", errors="replace")
                if ct == "text/plain":
                    body = text
                    break
                if ct == "text/html" and not body:
                    body = _strip_html(text)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(msg.get_content_charset("utf-8") or "utf-8", errors="replace")
                body = text if msg.get_content_type() == "text/plain" else _strip_html(text)

        if not body:
            log.warning("[gmail-imap] newsletter found but body is empty: %s", subject[:60])
            return None

        log.info("[gmail-imap] newsletter found: %s (%d chars)", subject[:60], len(body))
        return body

    except imaplib.IMAP4.error as exc:
        # Authentication failure -- clear message for logs
        msg_str = str(exc)
        if "AUTHENTICATIONFAILED" in msg_str or "Invalid credentials" in msg_str:
            log.error(
                "[gmail-imap] authentication failed -- check KAL_GMAIL_ADDRESS "
                "and KAL_GMAIL_APP_PASSWORD. App Password must be enabled at "
                "myaccount.google.com -> Security -> App Passwords"
            )
        else:
            log.warning("[gmail-imap] IMAP error: %s", exc)
        return None
    except Exception as exc:
        log.warning("[gmail-imap] fetch failed: %s", exc)
        return None


# ---- OAuth2 method (local development) --------------------------------------

def _build_gmail_service(credentials_path: str, token_path: str) -> Any:
    """
    Synchronous -- loads or creates OAuth2 credentials and returns a Gmail service.
    On first run (no token), opens a browser for one-time authorization.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Gmail OAuth2 packages not installed. Run: "
            "pip install google-auth google-auth-oauthlib google-api-python-client"
        ) from exc

    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            log.info("[gmail-oauth2] token refreshed")
        else:
            if not Path(credentials_path).exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {credentials_path}. "
                    "See bot/README.md for OAuth2 setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            log.info("[gmail-oauth2] authorization complete")
        Path(token_path).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _oauth2_fetch_sync(service: Any, sender_email: str) -> str | None:
    """Synchronous -- search Gmail API for today's newsletter."""
    today = datetime.date.today()
    query = f"from:{sender_email} after:{today.strftime('%Y/%m/%d')}"
    result = service.users().messages().list(userId="me", q=query, maxResults=3).execute()
    messages = result.get("messages", [])
    if not messages:
        log.debug("[gmail-oauth2] no newsletter today from %s", sender_email)
        return None

    msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="full"
    ).execute()
    body = _extract_body_from_payload(msg.get("payload", {}))
    if not body:
        log.warning("[gmail-oauth2] newsletter found but body is empty")
        return None

    subject = ""
    for header in msg.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "subject":
            subject = header.get("value", "")
            break
    log.info("[gmail-oauth2] newsletter found: %s (%d chars)", subject[:60], len(body))
    return body


# ---- Claude brief builder ----------------------------------------------------

BRIEF_SYSTEM = """\
You are Kal, an expert prediction market and financial analyst.
You read morning financial newsletters and distill them into fast, actionable
trading intelligence. Every point must have a market implication -- if it
doesn't affect markets, skip it. Write like a trader briefing another trader.
Fast, direct, no fluff. Under 15 words per bullet.
"""

BRIEF_PROMPT = """\
Today is {date}. Read this financial newsletter and produce a morning brief.

NEWSLETTER TEXT:
{newsletter}

TOP KALSHI MARKETS RIGHT NOW (for the Prediction Market Angle section):
{kalshi_block}

Produce EXACTLY this format -- every section is mandatory:

**Morning Brief -- {date}**
*What matters today. What's the trade.*

**MACRO**
- [Item] -- [market implication in under 15 words]
- [Item] -- [market implication in under 15 words]
(2-4 bullets, only items that move markets)

**CRYPTO**
- [Item] -- [1-line implication for BTC/ETH/SOL]
(1-3 bullets, crypto-specific news only)

**EARNINGS** (omit section entirely if no earnings in newsletter)
- [Company] beat/missed -- [one word reason]

**BIG MONEY MOVING**
- [Notable deal/funding/move] -- why it matters for markets
(1-3 bullets, significant capital flows only)

**TRADER'S ANGLE**
- Stocks: [Key movers, sectors showing strength/weakness]
- Crypto: [BTC/ETH/SOL overnight action, sentiment, key levels if mentioned]
- Commodities: [Oil, gold, silver -- significant moves only]
- Rates: [Bond market, yield curve, Fed implications if mentioned]
- Sectors: [Which sectors leading/lagging and why]
- Setup of the day: [One specific trade setup worth watching today]

**PREDICTION MARKET ANGLE**
[2-3 sentences connecting today's headlines to Kalshi prediction markets. Be specific -- name actual markets from the list above and their current prices. Explain why they might be mispriced given today's news.]

**TODAY'S FOCUS**
[1-2 sentences -- the single most important thing to watch today and why it matters for prediction markets specifically]

Rules:
- Every bullet must have a direct market implication -- skip anything else
- Prediction market angle is MANDATORY -- always connect news to Kalshi
- Keep each bullet under 15 words
- TODAY'S FOCUS must be prediction-market oriented
- If a section genuinely has no content from the newsletter, write "-- nothing notable today"
"""


async def build_morning_brief(
    newsletter_text: str,
    kalshi_markets: list[dict],
    model_override: str | None = None,
) -> tuple[str, float]:
    """Build the morning brief from newsletter text (ONE Claude call)."""
    from config import settings
    import anthropic

    date_str = datetime.datetime.now().strftime("%A, %B %-d")

    sorted_m = sorted(
        kalshi_markets,
        key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
        reverse=True,
    )[:8]
    kalshi_block = "\n".join(
        f"- {(m.get('title') or '?')[:65]} -- "
        f"{round(float(m.get('yes_ask_dollars', 0.5) or 0.5) * 100)}%"
        for m in sorted_m
    ) or "No markets loaded"

    newsletter_trimmed = newsletter_text[:8000]
    if len(newsletter_text) > 8000:
        newsletter_trimmed += "\n... [truncated]"

    prompt = BRIEF_PROMPT.format(
        date=date_str,
        newsletter=newsletter_trimmed,
        kalshi_block=kalshi_block,
    )

    active_model = model_override or settings.claude_model
    client  = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=active_model,
        max_tokens=1200,
        system=BRIEF_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    brief   = message.content[0].text.strip()
    in_tok  = message.usage.input_tokens
    out_tok = message.usage.output_tokens
    cost    = (
        in_tok * 0.80 / 1_000_000 + out_tok * 4.0 / 1_000_000
        if "haiku" in active_model.lower()
        else in_tok * 15.0 / 1_000_000 + out_tok * 75.0 / 1_000_000
    )

    log.info("[gmail] brief built: %d chars, cost=$%.4f, model=%s", len(brief), cost, active_model)
    return brief, round(cost, 6)


# ---- GmailReader orchestrator ------------------------------------------------

class GmailReader:
    """
    Fetches the morning newsletter and builds the morning brief.

    Auth priority:
      1. IMAP + App Password  -- if imap_address + imap_password both set
      2. OAuth2               -- if credentials_path file exists

    Tracks whether the brief has been posted today to avoid duplicates.
    """

    def __init__(
        self,
        credentials_path: str,
        token_path: str,
        imap_address: str = "",
        imap_password: str = "",
    ) -> None:
        self._creds_path   = str(_resolve_path(credentials_path))
        self._token_path   = str(_resolve_path(token_path))
        self._imap_address = imap_address.strip()
        self._imap_password = imap_password.strip()
        self._oauth2_service: Any = None
        self._posted_date: str = ""

    @property
    def _use_imap(self) -> bool:
        """True when App Password credentials are available -- prefer over OAuth2."""
        return bool(self._imap_address and self._imap_password)

    @property
    def is_configured(self) -> bool:
        """True if either auth method is available."""
        if self._use_imap:
            return True
        return Path(self._creds_path).exists()

    # ---- IMAP fetch ----------------------------------------------------------

    async def _fetch_imap(self, sender_email: str) -> str | None:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            partial(_imap_fetch_sync, self._imap_address, self._imap_password, sender_email),
        )

    # ---- OAuth2 fetch --------------------------------------------------------

    async def _get_oauth2_service(self) -> Any:
        if self._oauth2_service is not None:
            return self._oauth2_service
        loop = asyncio.get_event_loop()
        svc  = await loop.run_in_executor(
            None,
            partial(_build_gmail_service, self._creds_path, self._token_path),
        )
        self._oauth2_service = svc
        return svc

    async def _fetch_oauth2(self, sender_email: str) -> str | None:
        try:
            svc  = await self._get_oauth2_service()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                partial(_oauth2_fetch_sync, svc, sender_email),
            )
        except FileNotFoundError as exc:
            log.warning("[gmail-oauth2] %s", exc)
            return None
        except Exception as exc:
            log.warning("[gmail-oauth2] fetch failed: %s", exc)
            self._oauth2_service = None   # reset so next attempt retries auth
            return None

    # ---- Public API ----------------------------------------------------------

    async def fetch_newsletter(self, sender_email: str) -> str | None:
        """
        Fetch today's newsletter from sender_email.
        Uses IMAP (App Password) when configured, otherwise OAuth2.
        Returns body text or None.
        """
        if not self.is_configured:
            log.debug("[gmail] not configured -- no credentials found")
            return None

        if self._use_imap:
            log.debug("[gmail] using IMAP/App Password auth")
            return await self._fetch_imap(sender_email)
        else:
            log.debug("[gmail] using OAuth2 auth")
            return await self._fetch_oauth2(sender_email)

    def already_posted_today(self) -> bool:
        return self._posted_date == datetime.date.today().isoformat()

    def mark_posted(self) -> None:
        self._posted_date = datetime.date.today().isoformat()


# ---- Convenience export ------------------------------------------------------

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
