"""
targeted_cleanup.py — One-time targeted cleanup for specific channel issues.

Targets (read-only on all other channels):
  #morning-brief  Delete old-format briefs (contain "Overnight prices") and old guide
                  card ("5 financial newsletters"). Keep new format + new guide card.
  #trades         Delete old guide card ("Every trade Kal places and the result when it settles").
  #thesis         Delete all but the most-recent guide card.
  #ideas          Delete all but the most-recent guide card.
  #summary        Delete all embed-only bot messages (empty content, non-empty embeds)
                  from March 23 — these are the stale empty embeds.

Run:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python targeted_cleanup.py
"""
from __future__ import annotations

import asyncio
import datetime
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snowflake_to_utc(msg: dict) -> datetime.datetime:
    snowflake = int(msg["id"])
    ts_ms = (snowflake >> 22) + 1420070400000
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000)


def _preview(msg: dict) -> str:
    content = msg.get("content", "") or ""
    if not content and msg.get("embeds"):
        desc = (msg["embeds"][0].get("description") or "")[:80]
        return f"[embed] {desc!r}"
    return content[:100].replace("\n", " ")


async def _fetch_messages(bot, ch_id: int, limit: int = 100) -> list[dict]:
    from discord_bot import BASE_URL
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(
            f"{BASE_URL}/channels/{ch_id}/messages",
            headers=bot._headers,
            params={"limit": limit},
        )
    r.raise_for_status()
    return r.json()


async def _delete(bot, ch_id: int, msg_id: int, reason: str, dry_run: bool) -> None:
    from discord_bot import BASE_URL
    print(f"      DELETE [{reason}]  id={msg_id}")
    if dry_run:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.delete(
                f"{BASE_URL}/channels/{ch_id}/messages/{msg_id}",
                headers=bot._headers,
            )
        if r.status_code == 429:
            retry_after = float(r.headers.get("retry-after", 5))
            print(f"      [rate limit] waiting {retry_after:.0f}s...")
            await asyncio.sleep(retry_after)
            # retry once
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.delete(
                    f"{BASE_URL}/channels/{ch_id}/messages/{msg_id}",
                    headers=bot._headers,
                )
        if r.status_code not in (200, 204, 404):
            print(f"      [warn] delete returned {r.status_code}")
    except Exception as exc:
        print(f"      [error] {exc}")
    await asyncio.sleep(0.5)   # gentle rate limit


# ── Channel cleaners ──────────────────────────────────────────────────────────

async def clean_morning_brief(bot, ch_id: int, bot_id: int, dry_run: bool) -> int:
    """
    Keep:   guide card with "28 financial newsletters"
            brief message(s) containing "**The Big Picture**" (new format)
    Delete: guide card with "5 financial newsletters"
            brief message(s) containing "Overnight prices" (old format header)
    """
    print("\n#morning-brief")
    messages = await _fetch_messages(bot, ch_id, limit=50)
    deleted = 0

    for msg in messages:
        if int(msg.get("author", {}).get("id", 0)) != bot_id:
            continue
        content = msg.get("content", "") or ""
        msg_id  = int(msg["id"])
        ts      = _snowflake_to_utc(msg)

        # Old guide card — wrong newsletter count
        if content.startswith("**Kal — #morning-brief**") and "5 financial newsletters" in content:
            print(f"    {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")
            await _delete(bot, ch_id, msg_id, "old guide card (5 newsletters)", dry_run)
            deleted += 1
            continue

        # Old-format brief — has "Overnight prices:" section
        if "**Overnight prices:**" in content or "Overnight prices" in content:
            print(f"    {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")
            await _delete(bot, ch_id, msg_id, "old-format brief (Overnight prices)", dry_run)
            deleted += 1
            continue

        print(f"    KEEP  {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")

    return deleted


async def clean_trades(bot, ch_id: int, bot_id: int, dry_run: bool) -> int:
    """
    Delete: old guide card containing "Every trade Kal places and the result when it settles"
    Keep:   new guide card with "Open positions only"
    """
    print("\n#trades")
    messages = await _fetch_messages(bot, ch_id, limit=50)
    deleted = 0

    for msg in messages:
        if int(msg.get("author", {}).get("id", 0)) != bot_id:
            continue
        content = msg.get("content", "") or ""
        msg_id  = int(msg["id"])
        ts      = _snowflake_to_utc(msg)

        if (content.startswith("**Kal — #trades**")
                and "Every trade Kal places and the result when it settles" in content):
            print(f"    {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")
            await _delete(bot, ch_id, msg_id, "old guide card", dry_run)
            deleted += 1
            continue

        print(f"    KEEP  {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")

    return deleted


async def clean_keep_newest_guide(
    bot, ch_id: int, ch_name: str, bot_id: int,
    guide_fingerprint: str, dry_run: bool,
    keep_count: int = 1,
) -> int:
    """
    For channels where old and new guides share the same start prefix:
    keep the `keep_count` most-recent guide cards, delete all older ones.
    Messages are returned newest-first, so index 0 = most recent.
    Set keep_count=0 to delete all matching guide cards unconditionally.
    """
    print(f"\n#{ch_name}")
    messages = await _fetch_messages(bot, ch_id, limit=50)
    deleted = 0

    guide_msgs: list[dict] = []
    for msg in messages:
        if int(msg.get("author", {}).get("id", 0)) != bot_id:
            continue
        content = msg.get("content", "") or ""
        if content.startswith(guide_fingerprint):
            guide_msgs.append(msg)

    if not guide_msgs:
        print("    no guide cards found")
        return 0

    # Index 0 = newest (Discord returns newest-first)
    to_keep = guide_msgs[:keep_count]
    to_del  = guide_msgs[keep_count:]

    for msg in to_keep:
        keep_ts = _snowflake_to_utc(msg)
        print(f"    KEEP  {keep_ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")

    for msg in to_del:
        msg_id = int(msg["id"])
        ts     = _snowflake_to_utc(msg)
        print(f"    {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")
        await _delete(bot, ch_id, msg_id, "duplicate older guide", dry_run)
        deleted += 1

    return deleted


async def clean_summary_embeds(bot, ch_id: int, bot_id: int, dry_run: bool) -> int:
    """
    Delete embed-only bot messages (content == "", embeds present) from March 23.
    These are the stale empty performance embeds.
    Keep guide card and any real text messages.
    """
    print("\n#summary")
    messages = await _fetch_messages(bot, ch_id, limit=100)
    deleted  = 0
    cutoff   = datetime.datetime(2026, 3, 24, 0, 0, 0)  # anything before this = March 23

    for msg in messages:
        if int(msg.get("author", {}).get("id", 0)) != bot_id:
            continue
        content = msg.get("content", "") or ""
        embeds  = msg.get("embeds") or []
        msg_id  = int(msg["id"])
        ts      = _snowflake_to_utc(msg)

        # Guide card — always keep
        if content.startswith("**Kal — #summary**"):
            print(f"    KEEP (guide)  {ts.strftime('%H:%M UTC')}  {_preview(msg)[:60]}")
            continue

        # Embed-only message from March 23
        if not content and embeds and ts < cutoff:
            print(f"    {ts.strftime('%Y-%m-%d %H:%M UTC')}  {_preview(msg)[:60]}")
            await _delete(bot, ch_id, msg_id, "stale embed from March 23", dry_run)
            deleted += 1
            continue

        print(f"    KEEP  {ts.strftime('%Y-%m-%d %H:%M UTC')}  {_preview(msg)[:60]}")

    return deleted


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False) -> None:
    print()
    print("=" * 64)
    print("  KAL TARGETED CLEANUP")
    if dry_run:
        print("  *** DRY RUN — nothing will actually be deleted ***")
    print("=" * 64)

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
        print(f"\n[OK] Guild ID: {guild_id}   Bot ID: {bot_id}")
    except Exception as exc:
        print(f"[!!] Cannot connect: {exc}")
        sys.exit(1)

    from discord_bot import BASE_URL
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE_URL}/guilds/{guild_id}/channels", headers=bot._headers)
    r.raise_for_status()
    ch_map = {
        ch["name"].lower(): int(ch["id"])
        for ch in r.json()
        if ch.get("type") == 0
    }

    total_deleted = 0

    # 1. #morning-brief
    ch_id = ch_map.get("morning-brief")
    if ch_id:
        total_deleted += await clean_morning_brief(bot, ch_id, bot_id, dry_run)
    else:
        print("\n#morning-brief: NOT FOUND")

    # 2. #trades
    ch_id = ch_map.get("trades")
    if ch_id:
        total_deleted += await clean_trades(bot, ch_id, bot_id, dry_run)
    else:
        print("\n#trades: NOT FOUND")

    # 3. #thesis — keep newest guide, delete older duplicates
    ch_id = ch_map.get("thesis")
    if ch_id:
        total_deleted += await clean_keep_newest_guide(
            bot, ch_id, "thesis", bot_id, "**Kal — #thesis**", dry_run
        )
    else:
        print("\n#thesis: NOT FOUND")

    # 4. #ideas — delete legacy double-dash guide, then keep newest em-dash guide
    ch_id = ch_map.get("ideas")
    if ch_id:
        # First: delete any double-hyphen guide card ("Kal -- #ideas") unconditionally
        total_deleted += await clean_keep_newest_guide(
            bot, ch_id, "ideas (double-dash legacy)", bot_id, "**Kal -- #ideas**", dry_run,
            keep_count=0,
        )
        # Then: keep only the single newest em-dash guide
        total_deleted += await clean_keep_newest_guide(
            bot, ch_id, "ideas", bot_id, "**Kal — #ideas**", dry_run
        )
    else:
        print("\n#ideas: NOT FOUND")

    # 5. #summary — delete stale embed-only messages from March 23
    ch_id = ch_map.get("summary")
    if ch_id:
        total_deleted += await clean_summary_embeds(bot, ch_id, bot_id, dry_run)
    else:
        print("\n#summary: NOT FOUND")

    print()
    print("=" * 64)
    if dry_run:
        print(f"  DRY RUN COMPLETE — would delete {total_deleted} message(s)")
        print("  Re-run without --dry-run to apply changes.")
    else:
        print(f"  CLEANUP COMPLETE — deleted {total_deleted} message(s)")
    print("=" * 64)
    print()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry))
