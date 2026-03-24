"""
test_email.py -- Quick IMAP connectivity test for kalpredictslp@gmail.com.

Usage:
    python bot/test_email.py

Reads credentials from bot/.env (or environment variables).
Never modifies any email. Read-only.
"""
from __future__ import annotations

import datetime
import imaplib
import email as email_mod
from email.header import decode_header as _decode_header
from pathlib import Path


# ---- Load .env ---------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _decode_header_value(raw: str) -> str:
    parts = _decode_header(raw)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(data or "")
    return "".join(out)


# ---- Main test ---------------------------------------------------------------

def run_test(address: str, password: str, newsletter_senders: list[str]) -> None:
    host = "imap.gmail.com"
    port = 993

    print()
    print("=" * 60)
    print("  KAL EMAIL IMAP TEST")
    print("=" * 60)
    print(f"  Account : {address}")
    print(f"  Server  : {host}:{port}")
    print()

    # ---- Step 1: Connect & login ---------------------------------------------
    print("Step 1 — Connecting...")
    try:
        mail = imaplib.IMAP4_SSL(host, port)
    except Exception as exc:
        print(f"  [FAIL] Cannot reach {host}:{port} — {exc}")
        return

    try:
        mail.login(address, password)
        print(f"  [OK]   Logged in as {address}")
    except imaplib.IMAP4.error as exc:
        msg = str(exc)
        print(f"  [FAIL] Authentication failed: {msg}")
        if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
            print()
            print("  Gmail requires an App Password, not your regular password.")
            print("  Setup: myaccount.google.com -> Security -> App Passwords")
            print("         Select 'Mail' / 'Windows Computer' -> Generate")
        return

    # ---- Step 2: Select inbox ------------------------------------------------
    print()
    print("Step 2 — Opening INBOX...")
    status, data = mail.select("INBOX")
    if status != "OK":
        print(f"  [FAIL] Could not select INBOX: {data}")
        mail.logout()
        return
    msg_count = int(data[0])
    print(f"  [OK]   INBOX selected — {msg_count} total messages")

    # ---- Step 3: Last 5 emails -----------------------------------------------
    print()
    print("Step 3 — Last 5 emails in INBOX:")
    print(f"  {'#':<4} {'From':<40} {'Subject'}")
    print(f"  {'-'*4} {'-'*40} {'-'*35}")

    _, all_ids = mail.search(None, "ALL")
    msg_ids = all_ids[0].split()
    recent  = msg_ids[-5:] if len(msg_ids) >= 5 else msg_ids
    recent  = list(reversed(recent))   # newest first

    for i, mid in enumerate(recent, 1):
        _, msg_data = mail.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        raw_headers = msg_data[0][1]
        msg = email_mod.message_from_bytes(raw_headers)
        from_raw    = msg.get("From", "(unknown)")
        subject_raw = msg.get("Subject", "(no subject)")
        from_dec    = _decode_header_value(from_raw)[:40]
        subject_dec = _decode_header_value(subject_raw)[:45]
        print(f"  {i:<4} {from_dec:<40} {subject_dec}")

    # ---- Step 4: Newsletter sender check -------------------------------------
    print()
    print("Step 4 — Newsletter sender check (today's emails):")
    today     = datetime.date.today()
    since_str = today.strftime("%d-%b-%Y")
    print(f"  Searching for emails since {since_str}...")
    print()

    found_any = False
    for sender in newsletter_senders:
        _, data = mail.search(None, f'FROM "{sender}" SINCE "{since_str}"')
        msg_ids = data[0].split()
        if msg_ids:
            # Fetch subject of the most recent one
            _, msg_data = mail.fetch(msg_ids[-1], "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            raw_h   = msg_data[0][1]
            parsed  = email_mod.message_from_bytes(raw_h)
            subject = _decode_header_value(parsed.get("Subject", "(no subject)"))[:50]
            print(f"  [FOUND]   {sender}")
            print(f"            Subject: {subject}")
            found_any = True
        else:
            print(f"  [none ]   {sender}  -- no email today yet")
        print()

    mail.logout()

    # ---- Summary -------------------------------------------------------------
    print("=" * 60)
    if found_any:
        print("  RESULT: Connection OK. At least one newsletter found today.")
    else:
        print("  RESULT: Connection OK. No newsletters found yet today.")
        print("          (This is normal if it's early -- newsletters arrive")
        print("           between 5-8am ET. Kal checks every 10 min at 6am.)")
    print("=" * 60)
    print()


if __name__ == "__main__":
    env = _load_env()

    address  = env.get("KAL_EMAIL_ADDRESS", "").strip()
    password = env.get("KAL_EMAIL_PASSWORD", "").strip()

    if not address or not password:
        print()
        print("[!!] KAL_EMAIL_ADDRESS or KAL_EMAIL_PASSWORD not set in bot/.env")
        print("     Add them and re-run.")
        print()
        raise SystemExit(1)

    raw_senders   = env.get("NEWSLETTER_EMAILS", env.get("NEWSLETTER_EMAIL", ""))
    senders: list[str] = [s.strip() for s in raw_senders.split(",") if s.strip()]

    if not senders:
        print("[warn] NEWSLETTER_EMAILS not set — will only test connection")

    run_test(address, password, senders)
