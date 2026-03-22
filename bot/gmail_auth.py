"""
gmail_auth.py — One-time Gmail OAuth2 authorization for Kal.

Run this once to open the browser, authorize Gmail access, and save
the token. After this completes, the bot reads Gmail automatically.

Usage:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python gmail_auth.py
"""
import sys
from pathlib import Path

# ── Resolve paths relative to this file's directory ──────────────────────────
BOT_DIR      = Path(__file__).parent
CREDS_FILE   = BOT_DIR / "gmail_credentials.json"
TOKEN_FILE   = BOT_DIR / "gmail_token.json"
SCOPES       = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    print()
    print("=" * 60)
    print("KAL - GMAIL AUTHORIZATION")
    print("=" * 60)

    # ── Pre-flight: credentials file must exist ───────────────────
    if not CREDS_FILE.exists():
        print(f"\n[!!] gmail_credentials.json not found at:")
        print(f"     {CREDS_FILE}")
        print()
        print("Download it from Google Cloud Console:")
        print("  APIs & Services -> Credentials -> your OAuth client -> Download JSON")
        print("  Rename to gmail_credentials.json and place in the bot/ folder.")
        sys.exit(1)

    print(f"\n[OK] Credentials found: {CREDS_FILE}")

    # ── Check for existing token ──────────────────────────────────
    if TOKEN_FILE.exists():
        print(f"[OK] Token already exists: {TOKEN_FILE}")
        print()
        answer = input("A token already exists. Re-authorize anyway? [y/N]: ").strip().lower()
        if answer != "y":
            print("\nNo changes made. Existing token is still active.")
            print("If the bot is failing to read Gmail, delete gmail_token.json and re-run.")
            sys.exit(0)
        TOKEN_FILE.unlink()
        print("[..] Old token deleted. Starting fresh authorization.")

    # ── Import Google libraries ───────────────────────────────────
    print()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print("[!!] Google packages not installed. Run:")
        print("     pip install google-auth google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    # ── Run the OAuth2 flow ───────────────────────────────────────
    print("Opening browser for authorization...")
    print()
    print("In the browser:")
    print("  1. Sign in with the Gmail account that receives your newsletter")
    print('  2. If you see "Google hasn\'t verified this app" -> click Advanced -> Go to Kal Bot')
    print("  3. Click Continue to grant read-only Gmail access")
    print("  4. The browser will show 'The authentication flow has completed'")
    print()

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as exc:
        print(f"[!!] Authorization failed: {exc}")
        print()
        print("Common causes:")
        print("  - Your Gmail address was not added as a Test User in Google Cloud Console")
        print("    Fix: APIs & Services -> OAuth consent screen -> Test users -> Add your address")
        print("  - The credentials file is for the wrong project or app type")
        print("    Fix: re-download from the correct OAuth client (must be 'Desktop app' type)")
        sys.exit(1)

    # ── Save the token ────────────────────────────────────────────
    TOKEN_FILE.write_text(creds.to_json())

    print()
    print("=" * 60)
    print("[OK] Authorization complete.")
    print(f"[OK] Token saved to: {TOKEN_FILE}")
    print("=" * 60)
    print()

    # ── Quick sanity check — list the inbox label ─────────────────
    print("Verifying access with a quick Gmail API test...")
    try:
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email   = profile.get("emailAddress", "unknown")
        total   = profile.get("messagesTotal", 0)
        print(f"[OK] Connected as: {email}")
        print(f"[OK] Mailbox has {total:,} messages")
    except Exception as exc:
        print(f"[??] API test failed (token may still be valid): {exc}")

    print()
    print("Setup complete. The bot will now read Gmail automatically.")
    print("No browser prompt will appear again unless the token expires.")
    print()


if __name__ == "__main__":
    main()
