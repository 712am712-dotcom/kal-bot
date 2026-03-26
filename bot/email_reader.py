"""
email_reader.py -- Reads Kal's daily financial newsletters via IMAP.

Authentication priority:
  1. IMAP (primary, Railway / 24x7):
       Set KAL_EMAIL_ADDRESS and KAL_EMAIL_PASSWORD.
       Works with Gmail, Outlook, Yahoo, or any IMAP-enabled mailbox.
       No browser required. Works headless forever.
       IMAP server auto-detected from the email domain:
         @gmail.com                              -> imap.gmail.com:993
         @outlook.com / @hotmail.com / @live.com -> outlook.office365.com:993
         anything else                           -> outlook.office365.com:993

       Gmail note: You MUST use an App Password, not your regular password.
         1. Go to myaccount.google.com -> Security -> 2-Step Verification (enable)
         2. Go to myaccount.google.com -> Security -> App Passwords
         3. Select "Mail" / "Windows Computer" -> Generate
         4. Copy the 16-char password into KAL_EMAIL_PASSWORD

  2. OAuth2 (local development fallback):
       Set GMAIL_CREDENTIALS_PATH and GMAIL_TOKEN_PATH.
       First run opens browser for one-time authorization.

Multiple newsletters: set NEWSLETTER_EMAILS as a comma-separated list.
All newsletters found are fetched in ONE IMAP connection and synthesized
into a single morning brief with ONE Claude call.

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

from claude_client import KAL_IDENTITY

log = logging.getLogger(__name__)

SCOPES   = ["https://www.googleapis.com/auth/gmail.readonly"]
_BOT_DIR = Path(__file__).parent

# IMAP server lookup by domain
_IMAP_SERVERS: dict[str, tuple[str, int]] = {
    "gmail.com":    ("imap.gmail.com",         993),
    "outlook.com":  ("outlook.office365.com",  993),
    "hotmail.com":  ("outlook.office365.com",  993),
    "live.com":     ("outlook.office365.com",  993),
    "yahoo.com":    ("imap.mail.yahoo.com",     993),
}
_DEFAULT_IMAP_SERVER = ("outlook.office365.com", 993)


def _imap_server_for(email_address: str) -> tuple[str, int]:
    domain = email_address.lower().split("@")[-1] if "@" in email_address else ""
    return _IMAP_SERVERS.get(domain, _DEFAULT_IMAP_SERVER)


def _resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return _BOT_DIR / path.name


# ---- HTML / body helpers -----------------------------------------------------

def _strip_html(raw_html: str) -> str:
    raw_html = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r"<style[^>]*>.*?</style>",   " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    raw_html = re.sub(r"<br\s*/?>",                 "\n", raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r"</(p|div|li|tr|h[1-6])>",  "\n", raw_html, flags=re.IGNORECASE)
    raw_html = re.sub(r"<[^>]+>", " ", raw_html)
    text  = html_module.unescape(raw_html)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _decode_body_data(data: str) -> str:
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


def _parse_body_from_message(msg: email_mod.message.Message) -> str:
    """Extract plain-text body from a parsed email.message.Message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct      = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset("utf-8") or "utf-8"
            text    = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                body = text
                break
            if ct == "text/html" and not body:
                body = _strip_html(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset("utf-8") or "utf-8"
            text    = payload.decode(charset, errors="replace")
            body    = text if msg.get_content_type() == "text/plain" else _strip_html(text)
    return body


# ---- IMAP batch fetch (one connection, all senders) --------------------------

def _imap_fetch_all_sync(
    email_address: str,
    password: str,
    senders: list[str],
) -> dict[str, str]:
    """
    Open ONE IMAP connection, search for today's email from each sender,
    return {sender_email: body_text} for every newsletter found.
    Skips senders with no email today -- never fails the whole batch.
    """
    host, port = _imap_server_for(email_address)
    today      = datetime.date.today()
    since_str  = today.strftime("%d-%b-%Y")   # IMAP: 23-Mar-2026
    results: dict[str, str] = {}

    try:
        with imaplib.IMAP4_SSL(host, port) as mail:
            mail.login(email_address, password)
            mail.select("INBOX")

            for sender in senders:
                try:
                    _, data = mail.search(None, f'FROM "{sender}" SINCE "{since_str}"')
                    msg_ids = data[0].split()
                    if not msg_ids:
                        log.debug("[email-imap] no email from %s since %s", sender, since_str)
                        continue

                    # Fetch most recent match
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

                    body = _parse_body_from_message(msg)
                    if not body:
                        log.warning("[email-imap] empty body from %s: %s", sender, subject[:60])
                        continue

                    log.info("[email-imap] found from %s: %s (%d chars)", sender, subject[:60], len(body))
                    results[sender] = body

                except Exception as exc:
                    log.warning("[email-imap] error fetching from %s: %s", sender, exc)
                    continue

    except imaplib.IMAP4.error as exc:
        msg_str = str(exc)
        if any(k in msg_str for k in ("AUTHENTICATIONFAILED", "Invalid credentials", "Authentication unsuccessful")):
            log.error(
                "[email-imap] authentication failed for %s on %s -- "
                "check KAL_EMAIL_ADDRESS and KAL_EMAIL_PASSWORD. "
                "Gmail requires an App Password (not your regular password): "
                "myaccount.google.com -> Security -> App Passwords",
                email_address, host,
            )
        else:
            log.warning("[email-imap] IMAP error on %s: %s", host, exc)
    except Exception as exc:
        log.warning("[email-imap] connection failed to %s: %s", host, exc)

    return results


# ---- OAuth2 method (local development fallback) ------------------------------

def _build_gmail_service(credentials_path: str, token_path: str) -> Any:
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
    log.info("[gmail-oauth2] found from %s: %s (%d chars)", sender_email, subject[:60], len(body))
    return body


def _oauth2_fetch_all_sync(service: Any, senders: list[str]) -> dict[str, str]:
    """Fetch all senders via OAuth2. Returns {sender: body}."""
    results: dict[str, str] = {}
    for sender in senders:
        try:
            body = _oauth2_fetch_sync(service, sender)
            if body:
                results[sender] = body
        except Exception as exc:
            log.warning("[gmail-oauth2] error fetching from %s: %s", sender, exc)
    return results


# ---- Claude brief builder ----------------------------------------------------

BRIEF_SYSTEM = KAL_IDENTITY + """
Your current task: Synthesize today's financial newsletters into one fast, actionable morning brief.
You read so your CEO doesn't have to. Every item must answer: what does this mean for markets?
Write like a trader briefing another trader. Fast, direct, no fluff. Under 15 words per bullet.
"""

BRIEF_PROMPT = """\
Today is {date}. Below are today's financial newsletters. Read all of them,
de-duplicate overlapping stories, and synthesize into ONE unified morning brief.

{newsletter_block}

TOP KALSHI MARKETS RIGHT NOW:
{kalshi_block}

Produce EXACTLY this format -- every section is mandatory:

**Morning Brief -- {date}**
*What matters today. What's the trade.*

**MACRO**
- [Item] -- [market implication in under 15 words]
(2-4 bullets, only items that actually move markets)

**CRYPTO**
- [Item] -- [1-line implication for BTC/ETH/SOL]
(1-3 bullets, crypto-specific news only)

**AI & TECH**
- [Item] -- [market impact in under 15 words]
(1-3 bullets, major AI/tech developments that affect markets)

**BIG MONEY**
- [Notable deal/funding/institutional flow] -- why it matters
(1-3 bullets, significant capital movements only)

**TRADER'S ANGLE**
- Stocks: [Key movers, sectors showing strength/weakness]
- Crypto: [BTC/ETH/SOL overnight action, sentiment, key levels]
- Commodities: [Oil, gold, silver -- significant moves only]
- Rates: [Bond market, yield curve, Fed implications]
- Sectors: [Which sectors leading/lagging and why]
- Setup of the day: [One specific trade setup worth watching today]

**PREDICTION MARKET ANGLE**
[2-3 sentences connecting today's headlines to Kalshi prediction markets.
Name actual markets from the list above with their current prices.
Explain why they might be mispriced given today's news.]

**TODAY'S FOCUS**
[1-2 sentences -- the single most important thing to watch today and why
it matters specifically for prediction markets]

Rules:
- Every bullet must have a direct market implication -- skip anything that doesn't
- De-duplicate: if multiple newsletters cover the same story, combine into one bullet
- PREDICTION MARKET ANGLE is MANDATORY -- always connect news to Kalshi
- Keep each bullet under 15 words
- TODAY'S FOCUS must be prediction-market oriented
- If a section genuinely has no content, write "-- nothing notable today"
"""


async def build_morning_brief(
    newsletters: dict[str, str] | str,
    kalshi_markets: list[dict],
    model_override: str | None = None,
) -> tuple[str, float]:
    """
    Build the synthesized morning brief from one or more newsletters.

    newsletters: dict {sender -> body_text} for multi-newsletter mode,
                 or a plain str for backward compat (treated as single newsletter).
    """
    from config import settings
    import anthropic

    date_str = datetime.datetime.now().strftime("%A, %B %-d")

    # Normalize to dict
    if isinstance(newsletters, str):
        newsletters = {"newsletter": newsletters}

    # Build the newsletter block with clear separators
    parts: list[str] = []
    for i, (sender, body) in enumerate(newsletters.items(), 1):
        trimmed = body[:6000]
        if len(body) > 6000:
            trimmed += "\n... [truncated]"
        label = f"NEWSLETTER {i} (from {sender})"
        parts.append(f"{'=' * 60}\n{label}\n{'=' * 60}\n{trimmed}")
    newsletter_block = "\n\n".join(parts)

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

    prompt = BRIEF_PROMPT.format(
        date=date_str,
        newsletter_block=newsletter_block,
        kalshi_block=kalshi_block,
    )

    active_model = model_override or settings.claude_model
    client  = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=active_model,
        max_tokens=1400,
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

    n = len(newsletters)
    log.info(
        "[email] brief built from %d newsletter(s): %d chars, cost=$%.4f, model=%s",
        n, len(brief), cost, active_model,
    )
    return brief, round(cost, 6)


# ---- EmailReader orchestrator ------------------------------------------------

class EmailReader:
    """
    Fetches morning newsletters (one or many senders) and builds the brief.

    Auth priority:
      1. IMAP (KAL_EMAIL_ADDRESS + KAL_EMAIL_PASSWORD) -- Outlook, Gmail, any.
         Opens ONE connection per check. Fetches ALL senders in that connection.
      2. OAuth2 (GMAIL_CREDENTIALS_PATH) -- local dev fallback.

    Tracks whether brief has been posted today to avoid duplicates.
    """

    def __init__(
        self,
        credentials_path: str = "./gmail_credentials.json",
        token_path: str = "./gmail_token.json",
        imap_address: str = "",
        imap_password: str = "",
    ) -> None:
        self._creds_path    = str(_resolve_path(credentials_path))
        self._token_path    = str(_resolve_path(token_path))
        self._imap_address  = imap_address.strip()
        self._imap_password = imap_password.strip()
        self._oauth2_service: Any = None
        self._posted_date: str = ""

    @property
    def _use_imap(self) -> bool:
        return bool(self._imap_address and self._imap_password)

    @property
    def is_configured(self) -> bool:
        return self._use_imap or Path(self._creds_path).exists()

    # ---- Fetch all senders ---------------------------------------------------

    async def fetch_all_newsletters(self, senders: list[str]) -> dict[str, str]:
        """
        Fetch today's newsletter from every sender in the list.
        Returns {sender: body_text} for each one found (missing = not in dict).
        Single IMAP connection for all senders.
        """
        if not self.is_configured or not senders:
            return {}

        if self._use_imap:
            host, _ = _imap_server_for(self._imap_address)
            log.debug(
                "[email] IMAP fetch for %d senders via %s (%s)",
                len(senders), host, self._imap_address,
            )
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                partial(_imap_fetch_all_sync, self._imap_address, self._imap_password, senders),
            )
        else:
            return await self._fetch_all_oauth2(senders)

    async def _fetch_all_oauth2(self, senders: list[str]) -> dict[str, str]:
        try:
            if self._oauth2_service is None:
                loop = asyncio.get_event_loop()
                self._oauth2_service = await loop.run_in_executor(
                    None,
                    partial(_build_gmail_service, self._creds_path, self._token_path),
                )
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                partial(_oauth2_fetch_all_sync, self._oauth2_service, senders),
            )
        except FileNotFoundError as exc:
            log.warning("[gmail-oauth2] %s", exc)
            return {}
        except Exception as exc:
            log.warning("[gmail-oauth2] fetch failed: %s", exc)
            self._oauth2_service = None
            return {}

    # ---- Single-sender convenience (backward compat) -------------------------

    async def fetch_newsletter(self, sender_email: str) -> str | None:
        """Fetch a single sender. Returns body or None."""
        results = await self.fetch_all_newsletters([sender_email])
        return results.get(sender_email)

    # ---- State ---------------------------------------------------------------

    def already_posted_today(self) -> bool:
        return self._posted_date == datetime.date.today().isoformat()

    def mark_posted(self) -> None:
        self._posted_date = datetime.date.today().isoformat()


# Backward-compatible alias
GmailReader = EmailReader


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
