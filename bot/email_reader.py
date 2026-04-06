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
into a single morning brief with ONE Claude call. Supports 20+ sources.

Domain wildcard: add "@axios.com" to NEWSLETTER_EMAILS to catch any sender
at that domain (useful for newsletters that rotate send addresses).

Axios breaking news: separate from the morning brief. Kal checks every 30
minutes throughout the day for new Axios alert emails and evaluates each one
for market implications. Max 5 Discord posts per day. One Claude call per alert.
Subject-line pre-filter avoids Claude calls on obviously non-market content.

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

# ---- Axios breaking news state -----------------------------------------------
_processed_alert_uids: set[str] = set()
_processed_alert_date: str = ""
_breaking_count: int = 0
_breaking_date: str = ""
MAX_BREAKING_PER_DAY = 5


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
                    # Domain wildcard: "@axios.com" → search FROM "axios.com"
                    search_term = sender[1:] if sender.startswith("@") else sender
                    _, data = mail.search(None, f'FROM "{search_term}" SINCE "{since_str}"')
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


# ---- IMAP alert fetch (Axios breaking news) ----------------------------------

def _imap_fetch_recent_alerts_sync(
    email_address: str,
    password: str,
    domain: str = "@axios.com",
    processed_uids: frozenset[str] = frozenset(),
) -> list[dict]:
    """
    Fetch all unprocessed today's emails from a sender domain.
    domain: e.g. "@axios.com" — @ prefix is stripped for IMAP FROM search.
    Returns list of {uid, subject, from, body} for new (unprocessed) emails only.
    """
    host, port = _imap_server_for(email_address)
    today     = datetime.date.today()
    since_str = today.strftime("%d-%b-%Y")
    search_domain = domain.lstrip("@")
    results: list[dict] = []

    try:
        with imaplib.IMAP4_SSL(host, port) as mail:
            mail.login(email_address, password)
            mail.select("INBOX")

            _, data = mail.search(None, f'FROM "{search_domain}" SINCE "{since_str}"')
            msg_ids = data[0].split()
            if not msg_ids:
                return []

            for msg_id in msg_ids:
                try:
                    # Fetch UID for this sequence number
                    _, uid_data = mail.fetch(msg_id, "(UID)")
                    uid_raw = uid_data[0]
                    uid_str  = uid_raw.decode() if isinstance(uid_raw, bytes) else str(uid_raw)
                    uid_match = re.search(r"UID\s+(\d+)", uid_str)
                    uid = uid_match.group(1) if uid_match else msg_id.decode()

                    if uid in processed_uids:
                        continue

                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email_mod.message_from_bytes(raw)

                    subj_raw = _decode_header(msg.get("Subject", ""))[0]
                    subject  = (
                        subj_raw[0].decode(subj_raw[1] or "utf-8")
                        if isinstance(subj_raw[0], bytes)
                        else (subj_raw[0] or "")
                    )

                    from_addr = msg.get("From", "")
                    body      = _parse_body_from_message(msg)
                    if not body:
                        log.debug("[email-imap] empty body for alert uid=%s", uid)
                        continue

                    log.info("[email-imap] new alert uid=%s from=%s: %s", uid, from_addr[:40], subject[:60])
                    results.append({"uid": uid, "subject": subject, "from": from_addr, "body": body})

                except Exception as exc:
                    log.warning("[email-imap] alert uid parse error: %s", exc)
                    continue

    except imaplib.IMAP4.error as exc:
        log.warning("[email-imap] IMAP error fetching alerts from %s: %s", domain, exc)
    except Exception as exc:
        log.warning("[email-imap] connection failed fetching alerts: %s", exc)

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
You are a macro and market intelligence analyst.
Your scope: global markets, equities, commodities, macro events, earnings, M&A, trade setups.
You do NOT prioritize AI content unless it directly impacts markets. Signal quality over volume.

Your current task: Synthesize today's financial newsletters into one definitive morning brief.
You are the last mile. After reading this brief, the reader should never need to open the originals.
Write like a senior analyst at a top macro fund: precise, connected, no noise.
Every sentence serves one purpose: what is happening, why it matters, and what to watch.
Bonds and rates come first — they price everything else. Never skip yield moves or Fed commentary.
Never miss institutional flows, M&A above $500M, or sector rotations that are in the newsletters.
The test: if someone reads this brief and then opens the original newsletters and finds 5 things that matter that you missed — you failed.
Broadcast voice only. Never write "we", "let's build", "Day 1", or any motivational language.
Never use "significant" or "notable" — name the specific thing instead.
"""

BRIEF_PROMPT = """\
Today is {date}. Below are today's financial newsletters. Read ALL of them carefully.

{newsletter_block}

---
RSS INTELLIGENCE (live feeds — use to supplement newsletters, not replace them):
{rss_context_block}

---
TOP KALSHI MARKETS:
{kalshi_block}

---
Produce EXACTLY this format. Same structure every day. No extra sections. No deviations.

**Morning Brief — {date}**
*[One sentence — today's single narrative thread. What is the story connecting all of today's market themes? Not a list.]*

**Markets**
{markets_block}

**The Big Picture**
[2-3 sentences on today's macro picture. What is the market pricing in? What is the central tension or key question for traders today?
Bond and rate moves MUST be addressed here if yields moved or the Fed was mentioned — yields price everything else.
Never use "significant" or "notable" — be specific. No motivational language. No first-person plural.]

**What's Moving**
[4-8 bullets covering EVERY market-moving item from ALL newsletters today. Do not leave items out.
Format: [What happened] → [Why it matters for markets] → [Trade angle if any]
Mandatory coverage when present in newsletters: yield/rate moves, Fed commentary, institutional flows, M&A above $500M, crypto catalysts, sector rotations, geopolitical market impact.
The reader should never need to open an original newsletter after reading this section.]

**Deal Flow**
[3-5 most significant M&A, VC, IPO, or debt deals from today's newsletters. Only deals above $500M or genuinely market-moving smaller deals.
Format: [Company] [agreed to/raised/priced] [deal] at $[X]B — [one line: market or sector implication]
If fewer than 3 qualifying deals in today's newsletters, list what is there.
If no deals at all, write exactly: "No significant deal flow in today's newsletters."]

**The Trade Today**
[Single highest-conviction observation from today's news. One clear, direct paragraph — NOT a list.
Name the specific asset, direction, and thesis. Make it actionable.
End with a plain-English version in parentheses.]

**Watch List**
- [Specific item: name the data point, event, or price level — and exactly what signal you are watching for]
- [Second item, same format]
[Optional third item only if genuinely critical — never pad]
Maximum 3 items.

Hard rules:
- Include the Markets section data EXACTLY as provided above — do not alter any numbers or formatting
- De-duplicate stories that appear across multiple newsletters — combine into one bullet
- Never write "we", "let's", "Day 1", or any motivational language
- Never write "significant" or "notable" — name the specific thing instead
- Bond/rate coverage is mandatory when yields moved or the Fed was mentioned
- Deal Flow section is mandatory — always include it even if brief
- The reader must never need to open an original newsletter after reading this brief
"""


# ---- Axios breaking news evaluation -----------------------------------------

_LIKELY_MARKET = [
    "fed", "rate", "inflation", "cpi", "ppi", "gdp", "jobs", "employment",
    "unemployment", "recession", "market", "stock", "crypto", "bitcoin", "ethereum",
    "tariff", "trade war", "china", "oil", "energy", "bank", "banking", "debt",
    "treasury", "yield", "dollar", "currency", "earnings", "revenue", "profit",
    "layoffs", "merger", "acquisition", "ipo", "sec", "regulation",
    "geopolit", "war", "sanctions", "opec",
]


def _subject_worth_evaluating(subject: str) -> bool:
    """Return True if the subject line contains any market-relevant keyword."""
    s = subject.lower()
    return any(kw in s for kw in _LIKELY_MARKET)


AXIOS_ALERT_BREAK_SYSTEM = KAL_IDENTITY + """
Your current task: Evaluate a breaking news alert for market implications.
You are deciding whether this news is worth posting to #breaking-news.
Be fast and decisive — this is a routing decision, not a full analysis.
Respond ONLY with valid JSON, no preamble, no markdown.
"""

AXIOS_ALERT_PROMPT = """\
Breaking news alert received. Evaluate for market impact.

Subject: {subject}
From: {from_addr}
Body (first 600 chars):
{body}

Top Kalshi markets right now:
{kalshi_block}

Respond with ONLY this JSON (no preamble, no markdown code fences):
{{
  "has_market_impact": true/false,
  "routing": "business" | "tech" | "geopolitical" | "skip",
  "summary": "1-2 sentence summary of what happened and why it matters for markets",
  "market_angle": "1 sentence on the direct trading implication, or empty string if none",
  "kalshi_angle": "specific Kalshi market this could move, or empty string"
}}

Rules:
- has_market_impact: true only if this could move crypto, stocks, bonds, or commodities today
- routing "skip": politics, healthcare policy (unless clear market angle), sports, entertainment
- routing "business": corporate news, earnings, economic data, Fed, banking
- routing "tech": AI, semiconductors, big tech, cybersecurity breaches
- routing "geopolitical": trade wars, sanctions, military conflicts, energy supply shocks
- If routing is "skip", set has_market_impact to false
"""


async def evaluate_axios_alert(
    alert: dict,
    kalshi_markets: list[dict],
    anthropic_api_key: str,
    claude_model: str,
) -> tuple[str | None, float]:
    """
    Evaluate a breaking news alert for market impact.
    Returns (formatted_post, cost_dollars) or (None, cost) if not worth posting.
    cost is returned even when post is None so the caller can track spend.
    """
    subject = alert.get("subject", "")

    if not _subject_worth_evaluating(subject):
        log.debug("[axios-alert] pre-filtered (no market keywords): %s", subject[:80])
        return None, 0.0

    sorted_m = sorted(
        kalshi_markets,
        key=lambda m: float(m.get("volume_fp", 0) or 0) / 100.0,
        reverse=True,
    )[:5]
    kalshi_block = "\n".join(
        f"- {(m.get('title') or '?')[:60]} -- {round(float(m.get('yes_ask_dollars', 0.5) or 0.5) * 100)}%"
        for m in sorted_m
    ) or "No markets loaded"

    prompt = AXIOS_ALERT_PROMPT.format(
        subject=subject[:200],
        from_addr=alert.get("from", "")[:100],
        body=alert.get("body", "")[:600],
        kalshi_block=kalshi_block,
    )

    import time
    t0   = time.time()
    cost = 0.0
    try:
        import anthropic
        import json as _json
        client   = anthropic.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=claude_model,
            max_tokens=350,
            system=AXIOS_ALERT_BREAK_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        inp = response.usage.input_tokens
        out = response.usage.output_tokens
        cost = inp * 0.000015 + out * 0.000075
        log.info("[axios-alert] evaluated tokens=%d+%d cost=$%.4f ms=%d",
                 inp, out, cost, round((time.time() - t0) * 1000))

        # Strip markdown fences if model wrapped the JSON
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())

        result = _json.loads(raw)
        if not result.get("has_market_impact") or result.get("routing") == "skip":
            log.debug("[axios-alert] routing=skip or no impact: %s", subject[:80])
            return None, cost

        routing      = result.get("routing", "business").title()
        summary      = (result.get("summary") or "")[:300]
        market_angle = (result.get("market_angle") or "").strip()
        kalshi_angle = (result.get("kalshi_angle") or "").strip()

        lines = [
            f"**Breaking — {routing} Alert**",
            f"_{subject[:150]}_",
            "",
            summary,
        ]
        if market_angle:
            lines += ["", f"**Trade angle:** {market_angle}"]
        if kalshi_angle:
            lines.append(f"**Kalshi:** {kalshi_angle}")

        return "\n".join(lines), cost

    except Exception as exc:
        log.warning("[axios-alert] claude call failed: %s", exc)
        return None, cost


def _format_markets_block(market_data: dict) -> str:
    """
    Format pre-fetched market data into the Markets section lines.
    Pure Python — zero Claude calls. Called before the Claude prompt is built.
    """
    equities = market_data.get("equities", {})
    crypto   = market_data.get("crypto",   {})
    yields   = market_data.get("yields",   {})

    def _eq(sym: str, label: str) -> str:
        d = equities.get(sym, {})
        price = d.get("price")
        chg_p = d.get("change_p")
        if not price:
            return f"{label}: N/A"
        chg_str = f" ({chg_p:+.1f}%)" if chg_p is not None else ""
        return f"{label}: ${price:,.2f}{chg_str}"

    def _cr(sym: str) -> str:
        d = crypto.get(sym, {})
        price = d.get("price")
        chg   = d.get("change_24h")
        if not price:
            return f"{sym}: N/A"
        chg_str = f" ({chg:+.1f}%)" if chg is not None else ""
        fmt = f"${price:,.0f}" if price >= 100 else f"${price:.2f}"
        return f"{sym}: {fmt}{chg_str}"

    def _co(sym: str, label: str) -> str:
        d = equities.get(sym, {})
        price = d.get("price")
        chg_p = d.get("change_p")
        if not price:
            return f"{label}: N/A"
        chg_str = f" ({chg_p:+.1f}%)" if chg_p is not None else ""
        return f"{label}: ${price:.2f}{chg_str}"

    y10   = yields.get("yield_10y")
    y2    = yields.get("yield_2y")
    curve = yields.get("yield_curve")

    y10_str = f"{y10:.2f}%" if y10 is not None else "N/A"
    y2_str  = f"{y2:.2f}%" if y2 is not None else "N/A"

    if curve is not None:
        curve_bps    = round(curve * 100)
        curve_status = "inverted" if curve < 0 else "normal"
        curve_str    = f"{curve_bps:+d} bps [{curve_status}]"
    else:
        curve_str = "N/A"

    return "\n".join([
        f"- {_eq('SPY', 'S&P 500')} | {_eq('QQQ', 'Nasdaq')} | {_eq('DIA', 'Dow')}",
        f"- {_cr('BTC')} | {_cr('ETH')} | {_cr('SOL')}",
        f"- {_co('GLD', 'Gold')} | {_co('USO', 'Oil')} | {_co('SLV', 'Silver')}",
        f"- 10Y yield: {y10_str} | 2Y yield: {y2_str}",
        f"- Yield curve (10Y-2Y): {curve_str}",
    ])


async def build_morning_brief(
    newsletters: dict[str, str] | str,
    kalshi_markets: list[dict],
    market_data: dict | None = None,
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

    # Build the Markets section from pre-fetched data (zero Claude calls for this)
    markets_block = _format_markets_block(market_data or {})

    # Load RSS context (today's evaluated articles — zero extra Claude calls)
    try:
        from rss_reader import load_rss_context_for_brief
        rss_context_block = load_rss_context_for_brief()
    except Exception:
        rss_context_block = "No RSS data available today."

    prompt = BRIEF_PROMPT.format(
        date=date_str,
        newsletter_block=newsletter_block,
        kalshi_block=kalshi_block,
        markets_block=markets_block,
        rss_context_block=rss_context_block,
    )

    active_model = model_override or settings.claude_model
    client  = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=active_model,
        max_tokens=2200,
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

    # ---- Axios breaking news alerts ------------------------------------------

    async def fetch_recent_axios_alerts(self) -> list[dict]:
        """
        Fetch unprocessed Axios alert emails from today.
        Returns list of {uid, subject, from, body} for new emails only.
        Resets processed UID tracking at midnight.
        """
        global _processed_alert_uids, _processed_alert_date

        if not self._use_imap:
            return []

        today = datetime.date.today().isoformat()
        if today != _processed_alert_date:
            _processed_alert_uids = set()
            _processed_alert_date = today

        loop   = asyncio.get_event_loop()
        alerts = await loop.run_in_executor(
            None,
            partial(
                _imap_fetch_recent_alerts_sync,
                self._imap_address,
                self._imap_password,
                "@axios.com",
                frozenset(_processed_alert_uids),
            ),
        )

        for alert in alerts:
            _processed_alert_uids.add(alert["uid"])

        return alerts


# Backward-compatible alias
GmailReader = EmailReader


# ---- Convenience export ------------------------------------------------------

def extract_todays_focus(brief: str) -> str:
    """Pull the 'The Trade Today' section from a brief for #intelligence-feed cross-post."""
    # New format: **The Trade Today**
    # Legacy format: **TODAY'S FOCUS**
    match = re.search(
        r"\*\*(?:The Trade Today|TODAY'S FOCUS)\*\*(.*?)(?=\*\*[A-Z]|\Z)",
        brief,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        return "**The Trade Today**\n" + match.group(1).strip()
    return ""
