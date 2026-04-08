"""
restructure_discord.py — One-time Discord server migration to new 6-section layout.

New structure:
  📡 KAL HQ              morning-brief · the-trade-today · market-pulse · breaking
                          intelligence-feed · ae-attention · alerts · system-logs
  💹 MARKETS FOR DUMMIES  mfd-the-trade-today · mfd-morning-brief · mfd-content-queue · mfd-published
  🤖 ARTIFICIAL EDUCATION ae-content-queue · ae-newsletter-queue · ae-published
  🏗️ BRAND PIPELINE       brand-ideas
  ⚙️ TRADING OPS          trades · thesis · patterns · crypto · economic-calendar
  📦 ARCHIVE              market-open · market-close · summary

What this script does (idempotent — safe to re-run):
  1. Fetches current guild channel list
  2. Creates all 6 new categories (skips if already exists)
  3. Renames: #attention → #ae-attention, #content-queue → #ae-content-queue
  4. Creates new channels: the-trade-today, mfd-the-trade-today, mfd-morning-brief,
     mfd-content-queue, mfd-published, intelligence-feed (if deleted), ae-newsletter-queue,
     ae-published, brand-ideas
  5. Moves all existing channels into their correct new categories
  6. Merges #breaking-news into #breaking by deleting #breaking-news
  7. Posts and pins guide cards to channels that don't have one yet
  8. Attempts to delete old empty categories

Run once after deploying the code changes:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python restructure_discord.py

Rate-limit behaviour:
  - DELAY_AFTER_CALL (2s) between every API call
  - DELAY_AFTER_CHANNEL (4s) after creating/moving each channel
  - DELAY_AFTER_CATEGORY (8s) after finishing each category block
  - 429 responses: waits Retry-After (min 30s) then retries up to 5 times
"""
from __future__ import annotations

import asyncio
import io
import sys
import time
from typing import Any

import httpx

# Force UTF-8 stdout so emoji in category names don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Timing ─────────────────────────────────────────────────────────────────────
DELAY_AFTER_CALL     = 2
DELAY_AFTER_CHANNEL  = 4
DELAY_AFTER_CATEGORY = 8
DELAY_ON_429         = 30

# ── Channels to rename: {current_name: new_name} ───────────────────────────────
RENAMES: dict[str, str] = {
    "attention":    "ae-attention",
    "content-queue": "ae-content-queue",
}

# ── Channels to delete after migration ────────────────────────────────────────
# breaking-news is merged into breaking; the others are legacy
CHANNELS_TO_DELETE: list[str] = [
    "breaking-news",       # merged → #breaking
    # Old category shells that get replaced:
    "content-output",      # replaced by ae-published
    "content-review",      # retired
    "performance",         # retired from FEEDBACK LOOP
    "wins",                # retired from FEEDBACK LOOP
    "misses",              # retired from FEEDBACK LOOP
]

# ── Old categories to delete when empty ────────────────────────────────────────
OLD_CATEGORIES: list[str] = [
    "📡 DAILY INTELLIGENCE",
    "🎬 CONTENT ENGINE",
    "📊 FEEDBACK LOOP",
    "⚙️ SYSTEM",
]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# ── 429-aware HTTP wrapper ─────────────────────────────────────────────────────

async def _api(bot: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    from discord_bot import BASE_URL
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.request(
                method, f"{BASE_URL}{path}",
                headers=bot._headers, **kwargs,
            )
        if r.status_code == 429:
            retry_after = float(r.headers.get("retry-after", DELAY_ON_429))
            wait = max(retry_after, DELAY_ON_429)
            print(f"  [{_ts()}] 429 rate-limit — waiting {wait:.0f}s (attempt {attempt+1}/5) ...")
            await asyncio.sleep(wait)
            continue
        return r
    # Final attempt — let the caller handle errors
    async with httpx.AsyncClient(timeout=20.0) as c:
        return await c.request(method, f"{BASE_URL}{path}", headers=bot._headers, **kwargs)


async def _sleep(secs: float, label: str = "") -> None:
    if label:
        print(f"  [{_ts()}] sleeping {secs}s {label}")
    await asyncio.sleep(secs)


# ── Guild channel listing (cached per call) ────────────────────────────────────

async def _list_channels(bot: Any, guild_id: int) -> list[dict]:
    r = await _api(bot, "GET", f"/guilds/{guild_id}/channels")
    r.raise_for_status()
    await _sleep(DELAY_AFTER_CALL)
    return r.json()


# ── Category ops ──────────────────────────────────────────────────────────────

async def find_or_create_category(bot: Any, guild_id: int, name: str) -> int:
    print(f"  [{_ts()}] Category: {name}")
    channels = await _list_channels(bot, guild_id)
    for ch in channels:
        if ch["type"] == 4 and ch["name"].lower() == name.lower():
            print(f"           → already exists (id={ch['id']})")
            return int(ch["id"])

    r = await _api(bot, "POST", f"/guilds/{guild_id}/channels", json={"name": name, "type": 4})
    r.raise_for_status()
    cat_id = int(r.json()["id"])
    print(f"           → created (id={cat_id})")
    await _sleep(DELAY_AFTER_CALL)
    return cat_id


# ── Channel ops ───────────────────────────────────────────────────────────────

async def find_channel_id(bot: Any, guild_id: int, name: str) -> int | None:
    """Return the channel ID for a text channel with the given name, or None."""
    channels = await _list_channels(bot, guild_id)
    for ch in channels:
        if ch["type"] == 0 and ch["name"].lower() == name.lower():
            return int(ch["id"])
    return None


async def rename_channel(bot: Any, ch_id: int, new_name: str) -> None:
    """PATCH a channel's name."""
    r = await _api(bot, "PATCH", f"/channels/{ch_id}", json={"name": new_name})
    if r.status_code not in (200, 204):
        print(f"    [!!] rename failed: {r.status_code} {r.text[:80]}")
        return
    print(f"    [{_ts()}] renamed → #{new_name}")
    await _sleep(DELAY_AFTER_CALL)


async def move_channel(bot: Any, ch_id: int, category_id: int, position: int, ch_name: str) -> None:
    """Move a channel into a category and set its position."""
    r = await _api(bot, "PATCH", f"/channels/{ch_id}", json={
        "parent_id": str(category_id),
        "position": position,
    })
    if r.status_code not in (200, 204):
        print(f"    [!!] move #{ch_name} failed: {r.status_code} {r.text[:80]}")
    else:
        print(f"    [{_ts()}] moved #{ch_name} into category (pos={position})")
    await _sleep(DELAY_AFTER_CALL)


async def create_channel(
    bot: Any, guild_id: int, name: str, category_id: int, position: int
) -> int:
    """Create a text channel if it doesn't exist; return its ID either way."""
    existing_id = await find_channel_id(bot, guild_id, name)
    if existing_id:
        print(f"    [{_ts()}] #{name} already exists (id={existing_id}) — moving")
        await move_channel(bot, existing_id, category_id, position, name)
        return existing_id

    r = await _api(bot, "POST", f"/guilds/{guild_id}/channels", json={
        "name": name,
        "type": 0,
        "parent_id": str(category_id),
        "position": position,
    })
    r.raise_for_status()
    ch_id = int(r.json()["id"])
    print(f"    [{_ts()}] created #{name} (id={ch_id})")
    await _sleep(DELAY_AFTER_CALL)
    return ch_id


async def delete_channel(bot: Any, guild_id: int, name: str) -> None:
    print(f"  [{_ts()}] Deleting #{name} ...")
    ch_id = await find_channel_id(bot, guild_id, name)
    if ch_id is None:
        print(f"         → not found / already gone")
        return
    r = await _api(bot, "DELETE", f"/channels/{ch_id}")
    if r.status_code in (200, 204):
        print(f"         → deleted (id={ch_id})")
    else:
        print(f"         → [!!] {r.status_code} {r.text[:80]}")
    await _sleep(DELAY_AFTER_CALL)


async def delete_category_if_empty(bot: Any, guild_id: int, name: str) -> None:
    """Delete a category only if it has no channels left inside it."""
    channels = await _list_channels(bot, guild_id)
    cat_id: int | None = None
    for ch in channels:
        if ch["type"] == 4 and ch["name"].lower() == name.lower():
            cat_id = int(ch["id"])
            break
    if cat_id is None:
        print(f"  [{_ts()}] Category '{name}' not found — skipping delete")
        return

    # Check if any text channels still reference this category
    children = [ch for ch in channels if ch.get("parent_id") == str(cat_id)]
    if children:
        ch_names = [ch["name"] for ch in children]
        print(f"  [{_ts()}] Category '{name}' still has channels {ch_names} — skipping delete")
        return

    r = await _api(bot, "DELETE", f"/channels/{cat_id}")
    if r.status_code in (200, 204):
        print(f"  [{_ts()}] Deleted empty category '{name}'")
    else:
        print(f"  [{_ts()}] [!!] Could not delete '{name}': {r.status_code}")
    await _sleep(DELAY_AFTER_CALL)


# ── Guide posting ─────────────────────────────────────────────────────────────

async def post_and_pin_guide(bot: Any, bot_id: int, ch_id: int, ch_name: str, guide: str) -> None:
    fingerprint = guide[:80]
    print(f"    [{_ts()}] Guide for #{ch_name} — checking...")
    r = await _api(bot, "GET", f"/channels/{ch_id}/messages", params={"limit": 50})
    await _sleep(DELAY_AFTER_CALL)

    if r.status_code == 200:
        for msg in r.json():
            if int(msg.get("author", {}).get("id", 0)) == bot_id:
                if msg.get("content", "").startswith(fingerprint):
                    print(f"             → guide already pinned, skipping")
                    return

    r2 = await _api(bot, "POST", f"/channels/{ch_id}/messages", json={"content": guide})
    if r2.status_code not in (200, 201):
        print(f"             → [!!] post failed: {r2.status_code} {r2.text[:80]}")
        return
    msg_id = int(r2.json()["id"])
    print(f"             → posted (msg_id={msg_id})")
    await _sleep(DELAY_AFTER_CALL)

    r3 = await _api(bot, "PUT", f"/channels/{ch_id}/pins/{msg_id}")
    if r3.status_code in (200, 204):
        print(f"             → pinned")
    else:
        print(f"             → [warn] pin failed: {r3.status_code}")
    await _sleep(DELAY_AFTER_CALL)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    from discord_bot import CATEGORIES, DiscordBot, make_kal_avatar
    from discord_notifier import (
        _GUIDE_MORNING_BRIEF,
        _GUIDE_THE_TRADE_TODAY,
        _GUIDE_BREAKING,
        _GUIDE_INTELLIGENCE_FEED,
        _GUIDE_ATTENTION,           # used for ae-attention
        _GUIDE_ALERTS,
        _GUIDE_SYSTEM_LOGS,
        _GUIDE_MFD_THE_TRADE_TODAY,
        _GUIDE_MFD_MORNING_BRIEF,
        _GUIDE_MFD_CONTENT_QUEUE,
        _GUIDE_MFD_PUBLISHED,
        _GUIDE_CONTENT_QUEUE,       # used for ae-content-queue
        _GUIDE_AE_NEWSLETTER_QUEUE,
        _GUIDE_AE_PUBLISHED,
        _GUIDE_BRAND_IDEAS,
        _GUIDE_PATTERNS,
    )

    guides: dict[str, str] = {
        # 📡 KAL HQ
        "morning-brief":       _GUIDE_MORNING_BRIEF,
        "the-trade-today":     _GUIDE_THE_TRADE_TODAY,
        "breaking":            _GUIDE_BREAKING,
        "intelligence-feed":   _GUIDE_INTELLIGENCE_FEED,
        "ae-attention":        _GUIDE_ATTENTION,
        "alerts":              _GUIDE_ALERTS,
        "system-logs":         _GUIDE_SYSTEM_LOGS,
        # 💹 MARKETS FOR DUMMIES
        "mfd-the-trade-today": _GUIDE_MFD_THE_TRADE_TODAY,
        "mfd-morning-brief":   _GUIDE_MFD_MORNING_BRIEF,
        "mfd-content-queue":   _GUIDE_MFD_CONTENT_QUEUE,
        "mfd-published":       _GUIDE_MFD_PUBLISHED,
        # 🤖 ARTIFICIAL EDUCATION
        "ae-content-queue":    _GUIDE_CONTENT_QUEUE,
        "ae-newsletter-queue": _GUIDE_AE_NEWSLETTER_QUEUE,
        "ae-published":        _GUIDE_AE_PUBLISHED,
        # 🏗️ BRAND PIPELINE
        "brand-ideas":         _GUIDE_BRAND_IDEAS,
        # ⚙️ TRADING OPS
        "patterns":            _GUIDE_PATTERNS,
    }

    print()
    print("=" * 68)
    print("  KAL DISCORD RESTRUCTURE — 6-SECTION MIGRATION")
    print(f"  started at {_ts()}")
    print("=" * 68)

    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config load failed: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set in .env")
        sys.exit(1)

    bot = DiscordBot(token)

    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
        await _sleep(DELAY_AFTER_CALL)
        print(f"[OK] Guild: {guild_id}   Bot: {bot_id}")
    except Exception as exc:
        print(f"[!!] Cannot connect to Discord: {exc}")
        sys.exit(1)

    # ── Step 1: Update bot profile ────────────────────────────────────────────
    print()
    print(f"[{_ts()}] Step 1 — Updating bot profile ...")
    try:
        avatar = make_kal_avatar()
        await bot.update_profile("Kal", avatar)
        await _sleep(DELAY_AFTER_CALL)
        print(f"[OK] Profile updated")
    except Exception as exc:
        print(f"[warn] Profile update failed (non-fatal): {exc}")

    # ── Step 2: Rename channels ───────────────────────────────────────────────
    print()
    print(f"[{_ts()}] Step 2 — Renaming channels ...")
    for old_name, new_name in RENAMES.items():
        print(f"  #{old_name} → #{new_name}")
        # Check if already renamed
        ch_id = await find_channel_id(bot, guild_id, new_name)
        if ch_id:
            print(f"    [{_ts()}] #{new_name} already exists — skipping rename")
            continue
        old_id = await find_channel_id(bot, guild_id, old_name)
        if old_id is None:
            print(f"    [{_ts()}] #{old_name} not found — may already be renamed or deleted")
            continue
        await rename_channel(bot, old_id, new_name)
        await _sleep(DELAY_AFTER_CHANNEL, f"(after rename #{new_name})")

    # ── Step 3: Create categories + channels, move existing ───────────────────
    print()
    n_cats = len(CATEGORIES)
    print(f"[{_ts()}] Step 3 — Building {n_cats} categories ...")

    channel_ids: dict[str, int] = {}
    position = 0

    for cat_idx, (cat_name, cat_channels) in enumerate(CATEGORIES):
        print()
        print(f"  ── {cat_name} ({len(cat_channels)} channels) ──")
        try:
            cat_id = await find_or_create_category(bot, guild_id, cat_name)
        except Exception as exc:
            print(f"  [!!] Failed to create category '{cat_name}': {exc}")
            sys.exit(1)
        await _sleep(DELAY_AFTER_CALL)

        for ch_name in cat_channels:
            try:
                ch_id = await create_channel(bot, guild_id, ch_name, cat_id, position)
                channel_ids[ch_name] = ch_id
                position += 1
            except Exception as exc:
                print(f"    [!!] Failed to create/move #{ch_name}: {exc}")
                continue

            guide = guides.get(ch_name)
            if guide:
                try:
                    await post_and_pin_guide(bot, bot_id, ch_id, ch_name, guide)
                except Exception as exc:
                    print(f"    [!!] Guide for #{ch_name} failed: {exc}")

            await _sleep(DELAY_AFTER_CHANNEL, f"(after #{ch_name})")

        if cat_idx < n_cats - 1:
            await _sleep(DELAY_AFTER_CATEGORY, f"(after {cat_name})")

    # ── Step 4: Delete old channels ───────────────────────────────────────────
    print()
    print(f"[{_ts()}] Step 4 — Removing retired channels ...")
    for ch_name in CHANNELS_TO_DELETE:
        try:
            await delete_channel(bot, guild_id, ch_name)
        except Exception as exc:
            print(f"  [!!] Could not delete #{ch_name}: {exc}")
        await _sleep(DELAY_AFTER_CALL)

    # ── Step 5: Remove old empty categories ───────────────────────────────────
    print()
    print(f"[{_ts()}] Step 5 — Cleaning up old categories ...")
    for cat_name in OLD_CATEGORIES:
        try:
            await delete_category_if_empty(bot, guild_id, cat_name)
        except Exception as exc:
            print(f"  [!!] Could not remove '{cat_name}': {exc}")
        await _sleep(DELAY_AFTER_CALL)

    # ── Done ──────────────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    print(f"  RESTRUCTURE COMPLETE — {_ts()}")
    print()
    print(f"  {len(channel_ids)}/{sum(len(c) for _, c in CATEGORIES)} channels ready")
    print()
    print("  New layout:")
    for cat_name, cat_channels in CATEGORIES:
        cat_label = cat_name.encode("ascii", "replace").decode().strip()
        ch_list = "  ".join(f"#{c}" for c in cat_channels)
        print(f"    [{cat_label}]")
        print(f"      {ch_list}")
    print()
    print("  Next steps:")
    print("    1. Add webhooks in Discord for new channels and paste URLs into .env")
    print("    2. Restart the bot: python main.py --paper")
    print("=" * 68)
    print()


if __name__ == "__main__":
    asyncio.run(main())
