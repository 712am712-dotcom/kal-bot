"""
restructure_discord.py -- One-time Discord server migration script.

What this does:
  1. Creates all 7 categories and 19 channels (idempotent -- skips existing)
  2. Posts and pins guide cards to each new channel
  3. Makes #ideas private (deny @everyone VIEW_CHANNEL)
  4. Deletes the old #analysis and #intelligence channels

Run once after updating discord_notifier.py and discord_bot.py:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python restructure_discord.py

Safe to re-run -- channel creation and guide posting are both idempotent.

Rate-limit behaviour:
  - 2s delay after every API call
  - 5s delay after creating each channel
  - 10s delay after finishing each category
  - 429 responses: wait Retry-After (min 30s) then retry automatically
"""
from __future__ import annotations

import asyncio
import sys
import time

import httpx

OLD_CHANNELS_TO_DELETE = ["analysis", "intelligence"]

# Timing constants
DELAY_AFTER_CALL     = 2
DELAY_AFTER_CHANNEL  = 5
DELAY_AFTER_CATEGORY = 10
DELAY_ON_429         = 30


def _ts() -> str:
    return time.strftime("%H:%M:%S")


# 429-aware HTTP wrapper

async def _api(bot, method: str, path: str, **kwargs) -> httpx.Response:
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
            print(f"  [{_ts()}] 429 rate limit -- waiting {wait:.0f}s before retry {attempt+1}/4 ...")
            await asyncio.sleep(wait)
            continue
        return r
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.request(method, f"{BASE_URL}{path}", headers=bot._headers, **kwargs)
    r.raise_for_status()
    return r


async def _sleep(secs: float, label: str = "") -> None:
    if label:
        print(f"  [{_ts()}] sleeping {secs}s {label}...")
    await asyncio.sleep(secs)


# Core operations

async def find_or_create_category(bot, guild_id: int, name: str) -> int:
    print(f"  [{_ts()}] Category: {name}")
    r = await _api(bot, "GET", f"/guilds/{guild_id}/channels")
    r.raise_for_status()
    await _sleep(DELAY_AFTER_CALL)

    for ch in r.json():
        if ch["type"] == 4 and ch["name"].lower() == name.lower():
            print(f"           -> already exists (id={ch['id']})")
            return int(ch["id"])

    r2 = await _api(bot, "POST", f"/guilds/{guild_id}/channels", json={"name": name, "type": 4})
    r2.raise_for_status()
    cat_id = int(r2.json()["id"])
    print(f"           -> created (id={cat_id})")
    await _sleep(DELAY_AFTER_CALL)
    return cat_id


async def find_or_create_channel(
    bot, guild_id: int, name: str, category_id: int, position: int,
    private: bool = False,
) -> int:
    print(f"    [{_ts()}] Channel: #{name}{' [PRIVATE]' if private else ''}")
    r = await _api(bot, "GET", f"/guilds/{guild_id}/channels")
    r.raise_for_status()
    await _sleep(DELAY_AFTER_CALL)

    for ch in r.json():
        if ch["type"] == 0 and ch["name"].lower() == name.lower():
            print(f"             -> already exists (id={ch['id']})")
            return int(ch["id"])

    payload: dict = {
        "name":      name,
        "type":      0,
        "parent_id": str(category_id),
        "position":  position,
    }
    if private:
        payload["permission_overwrites"] = [{
            "id":    str(guild_id),
            "type":  0,
            "allow": "0",
            "deny":  "1024",   # VIEW_CHANNEL bit
        }]

    r2 = await _api(bot, "POST", f"/guilds/{guild_id}/channels", json=payload)
    r2.raise_for_status()
    ch_id = int(r2.json()["id"])
    print(f"             -> created (id={ch_id})")
    await _sleep(DELAY_AFTER_CALL)
    return ch_id


async def post_and_pin_guide(bot, bot_id: int, ch_id: int, ch_name: str, guide: str) -> None:
    fingerprint = guide[:80]
    print(f"    [{_ts()}] Guide for #{ch_name}: checking...")
    r = await _api(bot, "GET", f"/channels/{ch_id}/messages", params={"limit": 50})
    await _sleep(DELAY_AFTER_CALL)

    if r.status_code == 200:
        for msg in r.json():
            if int(msg.get("author", {}).get("id", 0)) == bot_id:
                if msg.get("content", "").startswith(fingerprint):
                    print(f"             -> guide already pinned, skipping")
                    return

    r2 = await _api(bot, "POST", f"/channels/{ch_id}/messages", json={"content": guide})
    if r2.status_code not in (200, 201):
        print(f"             -> [!!] post failed: {r2.status_code} {r2.text[:80]}")
        return
    msg_id = int(r2.json()["id"])
    print(f"             -> posted (msg_id={msg_id})")
    await _sleep(DELAY_AFTER_CALL)

    r3 = await _api(bot, "PUT", f"/channels/{ch_id}/pins/{msg_id}")
    if r3.status_code in (200, 204):
        print(f"             -> pinned")
    else:
        print(f"             -> [warn] pin failed: {r3.status_code}")
    await _sleep(DELAY_AFTER_CALL)


async def delete_channel(bot, guild_id: int, name: str) -> None:
    print(f"  [{_ts()}] Deleting #{name}...")
    r = await _api(bot, "GET", f"/guilds/{guild_id}/channels")
    r.raise_for_status()
    await _sleep(DELAY_AFTER_CALL)

    ch_id = None
    for ch in r.json():
        if ch["type"] == 0 and ch["name"].lower() == name.lower():
            ch_id = int(ch["id"])
            break

    if ch_id is None:
        print(f"         -> not found, already deleted")
        return

    r2 = await _api(bot, "DELETE", f"/channels/{ch_id}")
    if r2.status_code in (200, 204):
        print(f"         -> deleted (id={ch_id})")
    else:
        print(f"         -> [!!] delete failed: {r2.status_code} {r2.text[:80]}")
    await _sleep(DELAY_AFTER_CALL)


# Main

async def main() -> None:
    from discord_bot import CATEGORIES, PRIVATE_CHANNELS, DiscordBot, make_kal_avatar
    from discord_notifier import (
        _GUIDE_MORNING_BRIEF, _GUIDE_BREAKING_NEWS, _GUIDE_BIG_MONEY, _GUIDE_THESIS,
        _GUIDE_ECONOMIC_CALENDAR, _GUIDE_MARKET_OPEN, _GUIDE_MARKET_CLOSE, _GUIDE_IDEAS,
        _GUIDE_TRADES, _GUIDE_WATCHLIST, _GUIDE_WEEKLY,
        _GUIDE_CRYPTO, _GUIDE_STOCKS, _GUIDE_PREDICTION_MARKETS, _GUIDE_COMMODITIES,
        _GUIDE_HIGH_CONVICTION, _GUIDE_INTELLIGENCE_FEED,
        _GUIDE_SUMMARY, _GUIDE_ALERTS,
    )

    guides = {
        # INTELLIGENCE
        "morning-brief":      _GUIDE_MORNING_BRIEF,
        "breaking-news":      _GUIDE_BREAKING_NEWS,
        "big-money":          _GUIDE_BIG_MONEY,
        "thesis":             _GUIDE_THESIS,
        # CALENDAR
        "economic-calendar":  _GUIDE_ECONOMIC_CALENDAR,
        # DAILY ROUTINE
        "market-open":        _GUIDE_MARKET_OPEN,
        "market-close":       _GUIDE_MARKET_CLOSE,
        "ideas":              _GUIDE_IDEAS,
        # MARKETS
        "trades":             _GUIDE_TRADES,
        "watchlist":          _GUIDE_WATCHLIST,
        "weekly-analysis":    _GUIDE_WEEKLY,
        # ASSET CLASSES
        "crypto":             _GUIDE_CRYPTO,
        "stocks":             _GUIDE_STOCKS,
        "prediction-markets": _GUIDE_PREDICTION_MARKETS,
        "commodities":        _GUIDE_COMMODITIES,
        # SIGNALS
        "high-conviction":    _GUIDE_HIGH_CONVICTION,
        "intelligence-feed":  _GUIDE_INTELLIGENCE_FEED,
        # SYSTEM
        "summary":            _GUIDE_SUMMARY,
        "alerts":             _GUIDE_ALERTS,
    }

    n_cats = len(CATEGORIES)
    n_chs  = sum(len(chs) for _, chs in CATEGORIES)

    print()
    print("=" * 64)
    print("  KAL DISCORD RESTRUCTURE")
    print(f"  started at {_ts()}")
    print("=" * 64)

    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config failed to load: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set")
        sys.exit(1)

    bot = DiscordBot(token)

    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
        await _sleep(DELAY_AFTER_CALL)
        print(f"[OK] Guild: {guild_id}   Bot: {bot_id}")
    except Exception as exc:
        print(f"[!!] Cannot connect: {exc}")
        sys.exit(1)

    # Step 1: Update bot profile
    print()
    print(f"[{_ts()}] Step 1 -- Updating bot profile...")
    try:
        avatar = make_kal_avatar()
        await bot.update_profile("Kal", avatar)
        await _sleep(DELAY_AFTER_CALL)
        print(f"[OK] Profile updated")
    except Exception as exc:
        print(f"[warn] Profile update failed (non-fatal): {exc}")

    # Step 2: Create categories + channels
    print()
    print(f"[{_ts()}] Step 2 -- Creating {n_cats} categories and {n_chs} channels...")
    print(f"         (2s/call, 5s/channel, 10s/category, 30s on 429)")
    print()

    channel_ids: dict[str, int] = {}
    position = 0

    for cat_idx, (cat_name, cat_channels) in enumerate(CATEGORIES):
        print(f"  -- Category {cat_idx+1}/{n_cats}: {cat_name} --")
        try:
            cat_id = await find_or_create_category(bot, guild_id, cat_name)
        except Exception as exc:
            print(f"  [!!] Failed to create category {cat_name}: {exc}")
            sys.exit(1)

        await _sleep(DELAY_AFTER_CALL)

        for ch_name in cat_channels:
            is_private = ch_name in PRIVATE_CHANNELS
            try:
                ch_id = await find_or_create_channel(
                    bot, guild_id, ch_name, cat_id, position,
                    private=is_private,
                )
                channel_ids[ch_name] = ch_id
                position += 1
            except Exception as exc:
                print(f"    [!!] Failed to create #{ch_name}: {exc}")
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

    # Step 3: Delete old channels
    print()
    print(f"[{_ts()}] Step 3 -- Deleting old channels...")
    for ch_name in OLD_CHANNELS_TO_DELETE:
        try:
            await delete_channel(bot, guild_id, ch_name)
        except Exception as exc:
            print(f"  [!!] Could not delete #{ch_name}: {exc}")
        await _sleep(DELAY_AFTER_CALL)

    # Done
    print()
    print("=" * 64)
    print(f"  RESTRUCTURE COMPLETE -- {_ts()}")
    print()
    print(f"  {len(channel_ids)}/{n_chs} channels ready")
    print()
    print("  New structure:")
    for cat_name, cat_channels in CATEGORIES:
        ch_list = " ".join(f"#{c}" for c in cat_channels)
        # Strip emoji for safe ASCII output
        cat_label = cat_name.encode("ascii", "ignore").decode().strip()
        print(f"    [{cat_label or cat_name}] {ch_list}")
    print()
    print("  Next: restart the bot -- python main.py --paper")
    print("=" * 64)
    print()


if __name__ == "__main__":
    asyncio.run(main())
