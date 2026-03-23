"""
repost_guides.py — Force-replace all guide cards with the current clean versions.

Deletes every existing guide card in every channel, then posts and pins the
new versions from discord_notifier.py.

Run with:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python -X utf8 repost_guides.py
"""
from __future__ import annotations

import asyncio
import sys
import time

import httpx

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

DELAY = 1.5  # seconds between API calls


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _is_guide(content: str) -> bool:
    return any(content.startswith(fp) for fp in GUIDE_FINGERPRINTS)


async def delete_existing_guides(bot, ch_id: int, ch_name: str, bot_id: int) -> None:
    """Fetch up to 200 messages, delete any guide cards from this bot."""
    all_msgs: list[dict] = []
    try:
        page1 = await bot.get_messages(ch_id, limit=100)
        all_msgs.extend(page1)
        if len(page1) == 100:
            oldest_id = page1[-1]["id"]
            from discord_bot import BASE_URL
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(
                    f"{BASE_URL}/channels/{ch_id}/messages",
                    headers=bot._headers,
                    params={"limit": 100, "before": oldest_id},
                )
                if r.status_code == 200:
                    all_msgs.extend(r.json())
    except Exception as exc:
        print(f"  [{_ts()}] #{ch_name}: fetch failed — {exc}")
        return

    to_delete = [
        int(m["id"])
        for m in all_msgs
        if int(m.get("author", {}).get("id", 0)) == bot_id
        and _is_guide(m.get("content", ""))
    ]

    if not to_delete:
        print(f"  [{_ts()}] #{ch_name}: no existing guides found")
        return

    print(f"  [{_ts()}] #{ch_name}: deleting {len(to_delete)} old guide(s)...")
    for mid in to_delete:
        # Unpin silently (may already be unpinned)
        try:
            await bot.unpin(ch_id, mid)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        await bot._delete_message(ch_id, mid)
        await asyncio.sleep(DELAY)


async def post_and_pin(bot, ch_id: int, ch_name: str, guide: str) -> None:
    """Post guide text and pin it."""
    try:
        msg_id = await bot.send(ch_id, {"content": guide})
        print(f"  [{_ts()}] #{ch_name}: posted (msg_id={msg_id})")
        await asyncio.sleep(DELAY)
    except Exception as exc:
        print(f"  [{_ts()}] #{ch_name}: post failed — {exc}")
        return

    try:
        await bot.pin(ch_id, msg_id)
        print(f"  [{_ts()}] #{ch_name}: pinned")
        await asyncio.sleep(DELAY)
    except Exception as exc:
        print(f"  [{_ts()}] #{ch_name}: pin failed — {exc}")


async def main() -> None:
    from discord_notifier import (
        _GUIDE_MORNING_BRIEF, _GUIDE_BREAKING_NEWS, _GUIDE_BIG_MONEY, _GUIDE_THESIS,
        _GUIDE_TRADES, _GUIDE_WATCHLIST, _GUIDE_WEEKLY,
        _GUIDE_CRYPTO, _GUIDE_STOCKS, _GUIDE_PREDICTION_MARKETS, _GUIDE_COMMODITIES,
        _GUIDE_HIGH_CONVICTION, _GUIDE_INTELLIGENCE_FEED,
        _GUIDE_SUMMARY, _GUIDE_ALERTS,
    )

    GUIDES = {
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

    print()
    print("=" * 60)
    print("KAL GUIDE REPOST")
    print(f"started at {_ts()}")
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

    all_channels = await bot._list_channels(guild_id)
    ch_map = {
        ch["name"].lower(): int(ch["id"])
        for ch in all_channels
        if ch.get("type") == 0
    }

    print()
    print("Step 1 — Deleting old guide cards...")
    print()

    for ch_name in GUIDES:
        ch_id = ch_map.get(ch_name)
        if ch_id is None:
            print(f"  [{_ts()}] #{ch_name}: not found — skipping")
            continue
        await delete_existing_guides(bot, ch_id, ch_name, bot_id)
        await asyncio.sleep(DELAY)

    print()
    print("Step 2 — Posting clean guide cards...")
    print()

    for ch_name, guide in GUIDES.items():
        ch_id = ch_map.get(ch_name)
        if ch_id is None:
            print(f"  [{_ts()}] #{ch_name}: not found — skipping")
            continue
        await post_and_pin(bot, ch_id, ch_name, guide)
        await asyncio.sleep(DELAY)

    print()
    print("=" * 60)
    print("REPOST COMPLETE")
    print(f"finished at {_ts()}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
