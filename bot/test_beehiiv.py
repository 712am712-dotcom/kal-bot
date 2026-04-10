"""
test_beehiiv.py — Manual Beehiiv API connectivity test.

Run locally:
    cd bot && python test_beehiiv.py

Run on Railway:
    railway run python bot/test_beehiiv.py

What it does:
  1. POSTs a minimal test draft to Beehiiv
  2. Prints the full HTTP response (status + body) — good or bad
  3. On success: prints the draft post ID so you can find it in the dashboard
  4. On failure: prints the exact error from Beehiiv

No Claude call. No Supabase. Just Beehiiv.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running from either the repo root or the bot/ directory
sys.path.insert(0, str(Path(__file__).parent))

import httpx


async def main() -> None:
    api_key        = os.getenv("BEEHIIV_API_KEY", "")
    publication_id = os.getenv("BEEHIIV_MFD_PUBLICATION_ID", "")

    if not api_key:
        print("ERROR: BEEHIIV_API_KEY not set in environment")
        sys.exit(1)
    if not publication_id:
        print("ERROR: BEEHIIV_MFD_PUBLICATION_ID not set in environment")
        sys.exit(1)

    # Normalize: Beehiiv V2 requires pub_ prefix
    if not publication_id.startswith("pub_"):
        publication_id = f"pub_{publication_id}"
    url = f"https://api.beehiiv.com/v2/publications/{publication_id}/posts"

    test_html = """<div style="max-width:600px;margin:0 auto;padding:0 20px;font-family:Georgia,serif;font-size:16px;line-height:1.7;color:#1a1a1a;">
<p style="font-size:11px;font-weight:bold;letter-spacing:0.1em;text-transform:uppercase;color:#888888;margin:28px 0 8px;">THE TRADE TODAY</p>
<div style="background:#f9fafb;border-left:3px solid #F1C40F;padding:14px 18px;margin:16px 0;">
<p style="margin:0 0 14px;">This is a <strong>test draft</strong> posted by Kal's MFD newsletter automation. If you can see this, the Beehiiv API connection is working.</p>
</div>
<p style="font-size:13px;color:#9ca3af;margin-top:36px;border-top:1px solid #e5e7eb;padding-top:16px;">Markets For Dummies — plain English, every trading day.</p>
</div>"""

    payload = {
        "title":        "🧪 [TEST] MFD Newsletter API Check",
        "subtitle":     "Automated connectivity test — safe to delete",
        "status":       "draft",
        "body_content": test_html,
    }

    print(f"POST {url}")
    print(f"Publication ID: {publication_id}")
    print(f"API key (first 8): {api_key[:8]}...")
    print()

    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        print(f"EXCEPTION: {exc}")
        sys.exit(1)

    print(f"HTTP {r.status_code}")
    print()

    try:
        body = r.json()
        print(json.dumps(body, indent=2))
    except Exception:
        print(r.text)

    if r.status_code in (200, 201):
        try:
            data = r.json()
            post_id = data.get("data", {}).get("id") or data.get("id", "?")
            print()
            print(f"SUCCESS — draft post ID: {post_id}")
            print("Check your Beehiiv dashboard > Posts > Drafts")
        except Exception:
            print("SUCCESS (could not parse post ID)")
    else:
        print()
        print("FAILED — see error above")
        print()
        print("Common causes:")
        print("  401 - API key invalid or lacks posts:write scope")
        print("  403 - API key does not have access to this publication")
        print("  404 - publication ID not found (check BEEHIIV_MFD_PUBLICATION_ID)")
        print("  422 - invalid payload (check required fields)")
        sys.exit(1)


asyncio.run(main())
