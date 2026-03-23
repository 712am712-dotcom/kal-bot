"""
restructure_discord.py — One-time Discord server migration script.

What this does:
  1. Creates all 5 new categories and 15 channels (idempotent — skips existing)
  2. Posts pinned guide cards to each new channel
  3. Deletes the old #analysis and #intelligence channels

Run ONCE after updating discord_notifier.py and discord_bot.py:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python restructure_discord.py

Safe to re-run — channel creation and guide posting are both idempotent.
"""
from __future__ import annotations

import asyncio
import sys

# Old channels to delete after new structure is created
OLD_CHANNELS_TO_DELETE = ["analysis", "intelligence"]


async def main() -> None:
    print()
    print("=" * 64)
    print("  KAL DISCORD RESTRUCTURE")
    print("=" * 64)

    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config failed to load: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set — cannot proceed")
        sys.exit(1)

    from discord_bot import DiscordBot, make_kal_avatar
    from discord_notifier import (
        _GUIDE_MORNING_BRIEF, _GUIDE_BREAKING_NEWS, _GUIDE_BIG_MONEY, _GUIDE_THESIS,
        _GUIDE_TRADES, _GUIDE_WATCHLIST, _GUIDE_WEEKLY,
        _GUIDE_CRYPTO, _GUIDE_STOCKS, _GUIDE_PREDICTION_MARKETS, _GUIDE_COMMODITIES,
        _GUIDE_HIGH_CONVICTION, _GUIDE_INTELLIGENCE_FEED,
        _GUIDE_SUMMARY, _GUIDE_ALERTS,
    )

    bot = DiscordBot(token)

    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
        print(f"[OK] Guild ID: {guild_id}")
        print(f"[OK] Bot ID:   {bot_id}")
    except Exception as exc:
        print(f"[!!] Failed to connect: {exc}")
        sys.exit(1)

    # ── Step 1: Update bot profile ─────────────────────────────────────────────
    print()
    print("Step 1 — Updating bot profile...")
    try:
        avatar = make_kal_avatar()
        await bot.update_profile("Kal", avatar)
        print("[OK] Profile updated")
    except Exception as exc:
        print(f"[warn] Profile update failed (non-fatal): {exc}")

    # ── Step 2: Create all new channels with guide cards ──────────────────────
    print()
    print("Step 2 — Creating categories and channels...")
    guides = {
        "morning-brief":      _GUIDE_MORNING_BRIEF,
        "breaking-news":      _GUIDE_BREAKING_NEWS,
        "big-money":          _GUIDE_BIG_MONEY,
        "thesis":             _GUIDE_THESIS,
        "trades":             _GUIDE_TRADES,
        "watchlist":          _GUIDE_WATCHLIST,
        "weekly-analysis":    _GUIDE_WEEKLY,
        "crypto":             _GUIDE_CRYPTO,
        "stocks":             _GUIDE_STOCKS,
        "prediction-markets": _GUIDE_PREDICTION_MARKETS,
        "commodities":        _GUIDE_COMMODITIES,
        "high-conviction":    _GUIDE_HIGH_CONVICTION,
        "intelligence-feed":  _GUIDE_INTELLIGENCE_FEED,
        "summary":            _GUIDE_SUMMARY,
        "alerts":             _GUIDE_ALERTS,
    }

    try:
        channel_ids = await bot.setup(guides)
        print(f"[OK] {len(channel_ids)} channels ready")
        for name, cid in channel_ids.items():
            print(f"       #{name:<22} {cid}")
    except Exception as exc:
        print(f"[!!] Channel setup failed: {exc}")
        sys.exit(1)

    # ── Step 3: Delete old channels ────────────────────────────────────────────
    print()
    print(f"Step 3 — Deleting old channels: {OLD_CHANNELS_TO_DELETE}...")

    try:
        all_channels = await bot._list_channels(guild_id)
    except Exception as exc:
        print(f"[!!] Could not list channels: {exc}")
        sys.exit(1)

    ch_map = {
        ch["name"].lower(): (int(ch["id"]), ch.get("type"))
        for ch in all_channels
    }

    for ch_name in OLD_CHANNELS_TO_DELETE:
        entry = ch_map.get(ch_name.lower())
        if entry is None:
            print(f"  #{ch_name}: not found — already deleted or renamed")
            continue
        ch_id, ch_type = entry
        if ch_type != 0:
            print(f"  #{ch_name}: skipping — not a text channel (type={ch_type})")
            continue
        try:
            import httpx
            from discord_bot import BASE_URL
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.delete(
                    f"{BASE_URL}/channels/{ch_id}",
                    headers=bot._headers,
                )
            if r.status_code in (200, 204):
                print(f"  [OK] #{ch_name} deleted")
            else:
                print(f"  [!!] #{ch_name}: DELETE returned {r.status_code} — {r.text[:100]}")
        except Exception as exc:
            print(f"  [!!] #{ch_name}: delete failed — {exc}")
        await asyncio.sleep(0.5)

    print()
    print("=" * 64)
    print("  RESTRUCTURE COMPLETE")
    print()
    print("  New structure:")
    print("    🧠 INTELLIGENCE  #morning-brief #breaking-news #big-money #thesis")
    print("    📊 MARKETS       #trades #watchlist #weekly-analysis")
    print("    📈 ASSET CLASSES #crypto #stocks #prediction-markets #commodities")
    print("    🚨 SIGNALS       #high-conviction #intelligence-feed")
    print("    ⚙️ SYSTEM        #summary #alerts")
    print()
    print("  Restart the bot to begin routing to the new channels:")
    print("    python main.py --paper")
    print("=" * 64)
    print()


if __name__ == "__main__":
    asyncio.run(main())
