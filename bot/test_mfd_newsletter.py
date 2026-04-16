"""
test_mfd_newsletter.py — Manual trigger for MFD newsletter generation.

Run on Railway:
    railway run python bot/test_mfd_newsletter.py

Run locally (needs .env):
    cd bot && python test_mfd_newsletter.py

Posts the generated draft directly to #mfd-newsletter-queue.
Does NOT skip the daily dedup guard — it always runs regardless.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
import supabase_logger as discord
import mfd_newsletter as _mfd


async def main() -> None:
    print("Generating MFD newsletter draft...", flush=True)

    result = await _mfd.generate_mfd_newsletter(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
    )

    if result is None:
        print("ERROR: generate_mfd_newsletter() returned None.", flush=True)
        print("Check: does bot_communications have content from the last 24h?", flush=True)
        sys.exit(1)

    print(f"Generated OK. Subject A: {result['subject_a']}", flush=True)
    print(f"HTML length: {len(result['html'])} chars", flush=True)
    print(f"Plain text length: {len(result.get('plain_text', ''))} chars", flush=True)

    # Verify template sections present
    sections = [
        "BEFORE THE BELL", "WHAT'S GOING ON", "MORE STORIES",
        "THE TRADE TODAY", "ONE THING TO WATCH",
    ]
    html_upper = result["html"].upper()
    for s in sections:
        if s in html_upper:
            print(f"  [OK] Section found: {s}", flush=True)
        else:
            print(f"  [MISSING] Section not found: {s}", flush=True)

    more_stories_count = result["html"].upper().count("▸") + result["html"].count("▸")
    print(f"  More Stories bullets: ~{more_stories_count}", flush=True)

    print("\nPosting to #mfd-newsletter-queue...", flush=True)
    await discord.notify_mfd_newsletter_draft(
        subject_a=result["subject_a"],
        subject_b=result["subject_b"],
        subject_c=result["subject_c"],
        preview_text=result["preview_text"],
        html=result["html"],
        plain_text=result.get("plain_text", ""),
    )

    await _mfd.log_newsletter_dispatch(subject=result["subject_a"], beehiiv_id=None)
    print("Done. Check #mfd-newsletter-queue.", flush=True)


asyncio.run(main())
