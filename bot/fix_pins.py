"""
fix_pins.py — Clean up pinned messages in every Kal Discord channel.

For each text channel in the server:
  1. Fetch all pinned messages
  2. Among bot-authored pins: keep the single most-recent guide card, unpin+delete all others
  3. Unpin (and delete) every message NOT from the bot — including any pinned by users like Boog

Run with:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python -X utf8 fix_pins.py
"""
from __future__ import annotations

import asyncio
import sys

GUIDE_FINGERPRINTS = [
    "**Kal — #morning-brief**",
    "**Kal — #breaking-news**",
    "**Kal — #big-money**",
    "**Kal — #thesis**",
    "**Kal — #trades**",
    "**Kal — #watchlist**",
    "**Kal — #weekly-analysis**",
    "**Kal — #crypto**",
    "**Kal — #stocks**",
    "**Kal — #prediction-markets**",
    "**Kal — #commodities**",
    "**Kal — #high-conviction**",
    "**Kal — #intelligence-feed**",
    "**Kal — #summary**",
    "**Kal — #alerts**",
    # Legacy
    "**Kal — Trades Channel Guide**",
    "**Kal — Analysis Channel Guide**",
    "**Kal — Summary Channel Guide**",
    "**Kal — Intelligence Channel Guide**",
    "**Kal — Morning Brief Channel Guide**",
    "**Kal — Morning Briefing Channel Guide**",
    "**Kal — Weekly Analysis Channel Guide**",
]


def _is_guide(content: str) -> bool:
    return any(content.startswith(fp) for fp in GUIDE_FINGERPRINTS)


async def fix_channel_pins(bot, ch_id: int, ch_name: str, bot_id: int) -> None:
    try:
        pins = await bot.get_pins(ch_id)
    except Exception as exc:
        print(f"  #{ch_name}: could not fetch pins — {exc}")
        return

    if not pins:
        print(f"  #{ch_name}: no pins")
        return

    # Sort newest-first by snowflake ID (higher ID = more recent)
    pins_sorted = sorted(pins, key=lambda m: int(m["id"]), reverse=True)

    bot_guides:    list[dict] = []
    bot_non_guide: list[dict] = []
    user_pins:     list[dict] = []

    for msg in pins_sorted:
        author_id = int(msg.get("author", {}).get("id", 0))
        author_name = msg.get("author", {}).get("username", "?")
        content = msg.get("content", "")
        if author_id == bot_id:
            if _is_guide(content):
                bot_guides.append(msg)
            else:
                bot_non_guide.append(msg)
        else:
            user_pins.append(msg)

    print(f"  #{ch_name}: {len(pins)} pin(s) — "
          f"{len(bot_guides)} guide(s), "
          f"{len(bot_non_guide)} other-bot, "
          f"{len(user_pins)} user pin(s)")

    to_unpin_and_delete: list[dict] = []

    # Keep index 0 (most recent guide), remove the rest
    if bot_guides:
        keep = bot_guides[0]
        print(f"    keeping guide {keep['id']} (most recent)")
        for dup in bot_guides[1:]:
            print(f"    unpin+delete duplicate guide {dup['id']}")
            to_unpin_and_delete.append(dup)
    else:
        print(f"    no guide card found — nothing to keep")

    for msg in bot_non_guide:
        print(f"    unpin+delete non-guide bot msg {msg['id']}: {msg.get('content','')[:60]!r}")
        to_unpin_and_delete.append(msg)

    for msg in user_pins:
        author_name = msg.get("author", {}).get("username", "?")
        print(f"    unpin+delete user pin from @{author_name} ({msg['id']}): {msg.get('content','')[:60]!r}")
        to_unpin_and_delete.append(msg)

    if not to_unpin_and_delete:
        print(f"    clean — nothing to do")
        return

    for msg in to_unpin_and_delete:
        msg_id  = int(msg["id"])
        ch_id_  = int(msg.get("channel_id") or ch_id)
        # Unpin first
        try:
            await bot.unpin(ch_id_, msg_id)
            await asyncio.sleep(0.5)
        except Exception as exc:
            print(f"    [warn] unpin {msg_id} failed: {exc}")
        # Then delete
        try:
            await bot._delete_message(ch_id_, msg_id)
            await asyncio.sleep(0.5)
        except Exception as exc:
            print(f"    [warn] delete {msg_id} failed: {exc}")

    print(f"    done — removed {len(to_unpin_and_delete)} pin(s)")


async def main() -> None:
    print()
    print("=" * 60)
    print("KAL PIN CLEANUP")
    print("=" * 60)

    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config failed: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set")
        sys.exit(1)

    from discord_bot import DiscordBot
    bot = DiscordBot(token)

    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
        print(f"[OK] Guild: {guild_id}   Bot: {bot_id}")
    except Exception as exc:
        print(f"[!!] Cannot connect: {exc}")
        sys.exit(1)

    try:
        all_channels = await bot._list_channels(guild_id)
    except Exception as exc:
        print(f"[!!] Cannot list channels: {exc}")
        sys.exit(1)

    text_channels = [ch for ch in all_channels if ch.get("type") == 0]
    print(f"\nChecking pins in {len(text_channels)} text channels...\n")

    for ch in sorted(text_channels, key=lambda c: c.get("position", 0)):
        ch_id   = int(ch["id"])
        ch_name = ch["name"]
        await fix_channel_pins(bot, ch_id, ch_name, bot_id)
        await asyncio.sleep(1.0)

    print()
    print("=" * 60)
    print("PIN CLEANUP COMPLETE")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
