"""
cleanup_discord.py — One-time cleanup of duplicate Kal guide cards.

Deletes:
  - All duplicate guide card messages from every Kal channel
    (keeps the single most-recent guide per channel)
  - All "Please pin the guide above" messages (should never be visible)

Run once after updating discord_bot.py:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python cleanup_discord.py

Safe to re-run — already-deleted messages are silently skipped.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# ── Guide fingerprints (first 80 chars of each guide) ─────────────────────────
# These match what discord_notifier.py defines as _GUIDE_*.
# We identify Kal guide messages by checking whether the message starts with
# any of these strings.

GUIDE_FINGERPRINTS = [
    "**Kal — Trades Channel Guide**",
    "**Kal — Analysis Channel Guide**",
    "**Kal — Summary Channel Guide**",
    "**Kal — Intelligence Channel Guide**",
    "**Kal — Morning Brief Channel Guide**",
    "**Kal — Morning Briefing Channel Guide**",
    "**Kal — Weekly Analysis Channel Guide**",
]

# The "please pin" message text to hunt down and delete
PLEASE_PIN_TEXT = "Please pin the guide above"

# Old periodic summary message fingerprints (old format — delete all, keep none)
OLD_SUMMARY_FINGERPRINTS = [
    "**Update**",
    "**Update (paper)**",
]

CHANNELS_TO_CLEAN = [
    "trades",
    "analysis",
    "summary",
    "intelligence",
    "alerts",
    "morning-brief",
    "weekly-analysis",
]


def _is_guide(content: str) -> bool:
    return any(content.startswith(fp) for fp in GUIDE_FINGERPRINTS)


def _is_please_pin(content: str) -> bool:
    return PLEASE_PIN_TEXT in content


def _is_old_summary(content: str) -> bool:
    return any(content.startswith(fp) for fp in OLD_SUMMARY_FINGERPRINTS)


async def clean_channel(
    bot,
    ch_id: int,
    ch_name: str,
    bot_id: int,
) -> None:
    """
    For one channel:
      1. Fetch up to 200 recent messages (two pages of 100)
      2. Among bot messages: collect all guide messages and all please-pin messages
      3. Keep the single most-recent guide (lowest position in the list = most recent)
      4. Delete everything else
    """
    all_messages: list[dict] = []
    try:
        page1 = await bot.get_messages(ch_id, limit=100)
        all_messages.extend(page1)
        # If there are exactly 100, there may be older messages worth fetching
        if len(page1) == 100:
            oldest_id = page1[-1]["id"]
            try:
                import httpx
                from discord_bot import BASE_URL
                headers = bot._headers
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.get(
                        f"{BASE_URL}/channels/{ch_id}/messages",
                        headers=headers,
                        params={"limit": 100, "before": oldest_id},
                    )
                    if r.status_code == 200:
                        all_messages.extend(r.json())
            except Exception:
                pass
    except Exception as exc:
        print(f"  [!!] Could not fetch messages for #{ch_name}: {exc}")
        return

    # Separate into categories — only look at messages from this bot
    guide_msgs:      list[dict] = []
    please_pin_msgs: list[dict] = []

    old_summary_msgs: list[dict] = []

    for msg in all_messages:
        author_id = int(msg.get("author", {}).get("id", 0))
        if author_id != bot_id:
            continue
        content = msg.get("content", "")
        if _is_guide(content):
            guide_msgs.append(msg)
        elif _is_please_pin(content):
            please_pin_msgs.append(msg)
        elif _is_old_summary(content):
            old_summary_msgs.append(msg)

    # Messages are returned newest-first — index 0 is the most recent
    to_delete: list[int] = []

    if guide_msgs:
        # Keep index 0 (most recent), delete the rest
        keep    = guide_msgs[0]
        dupes   = guide_msgs[1:]
        keep_id = int(keep["id"])
        print(f"  #{ch_name}: keeping guide msg {keep_id}, deleting {len(dupes)} duplicate(s)")
        to_delete.extend(int(m["id"]) for m in dupes)
    else:
        print(f"  #{ch_name}: no guide messages found")

    if please_pin_msgs:
        print(f"  #{ch_name}: deleting {len(please_pin_msgs)} 'please pin' message(s)")
        to_delete.extend(int(m["id"]) for m in please_pin_msgs)

    if old_summary_msgs:
        print(f"  #{ch_name}: deleting {len(old_summary_msgs)} old summary message(s)")
        to_delete.extend(int(m["id"]) for m in old_summary_msgs)

    if not to_delete:
        print(f"  #{ch_name}: nothing to delete")
        return

    await bot.bulk_delete(ch_id, to_delete)
    print(f"  #{ch_name}: deleted {len(to_delete)} message(s)")


async def clean_intelligence_channel(
    bot,
    ch_id: int,
    ch_name: str,
    bot_id: int,
) -> None:
    """
    For #intelligence: delete ALL bot messages except the most recent guide card.
    This gives a clean slate for the new improved alert format.
    """
    all_messages: list[dict] = []
    try:
        page1 = await bot.get_messages(ch_id, limit=100)
        all_messages.extend(page1)
        if len(page1) == 100:
            oldest_id = page1[-1]["id"]
            try:
                import httpx
                from discord_bot import BASE_URL
                headers = bot._headers
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.get(
                        f"{BASE_URL}/channels/{ch_id}/messages",
                        headers=headers,
                        params={"limit": 100, "before": oldest_id},
                    )
                    if r.status_code == 200:
                        all_messages.extend(r.json())
            except Exception:
                pass
    except Exception as exc:
        print(f"  [!!] Could not fetch messages for #{ch_name}: {exc}")
        return

    guide_msgs:   list[dict] = []
    other_msgs:   list[dict] = []

    for msg in all_messages:
        author_id = int(msg.get("author", {}).get("id", 0))
        if author_id != bot_id:
            continue
        content = msg.get("content", "")
        if _is_guide(content):
            guide_msgs.append(msg)
        else:
            other_msgs.append(msg)

    to_delete: list[int] = [int(m["id"]) for m in other_msgs]

    if guide_msgs:
        keep = guide_msgs[0]
        dupes = guide_msgs[1:]
        print(f"  #{ch_name}: keeping guide msg {int(keep['id'])}, "
              f"deleting {len(other_msgs)} alert messages + {len(dupes)} dupe guide(s)")
        to_delete.extend(int(m["id"]) for m in dupes)
    else:
        print(f"  #{ch_name}: no guide found — deleting {len(other_msgs)} alert messages")

    if not to_delete:
        print(f"  #{ch_name}: nothing to delete")
        return

    await bot.bulk_delete(ch_id, to_delete)
    print(f"  #{ch_name}: deleted {len(to_delete)} message(s)")


async def main() -> None:
    print()
    print("=" * 60)
    print("KAL DISCORD CLEANUP")
    print("=" * 60)

    # Load config
    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config failed to load: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set in .env — cannot proceed")
        sys.exit(1)

    from discord_bot import DiscordBot
    bot = DiscordBot(token)

    # Resolve guild and bot identity
    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
        print(f"\n[OK] Guild ID: {guild_id}")
        print(f"[OK] Bot ID:   {bot_id}")
    except Exception as exc:
        print(f"[!!] Failed to connect to Discord: {exc}")
        sys.exit(1)

    # Get all channels in the guild
    try:
        all_channels = await bot._list_channels(guild_id)
    except Exception as exc:
        print(f"[!!] Failed to list channels: {exc}")
        sys.exit(1)

    ch_map = {
        ch["name"].lower(): int(ch["id"])
        for ch in all_channels
        if ch.get("type") == 0  # text channels only
    }

    print(f"\nCleaning {len(CHANNELS_TO_CLEAN)} channels...\n")

    for ch_name in CHANNELS_TO_CLEAN:
        ch_id = ch_map.get(ch_name.lower())
        if ch_id is None:
            print(f"  #{ch_name}: not found in server — skipping")
            continue
        # Full wipe on ALL channels — keep only the most-recent guide card, delete everything else
        await clean_intelligence_channel(bot, ch_id, ch_name, bot_id)
        await asyncio.sleep(1.0)  # gentle rate limit between channels

    print()
    print("=" * 60)
    print("CLEANUP COMPLETE")
    print()
    print("All channels wiped — each now has at most one guide card.")
    print("Restart the bot to confirm idempotent guide posting:")
    print("  python main.py --paper")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
