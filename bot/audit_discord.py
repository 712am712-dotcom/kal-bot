"""
audit_discord.py — Reads the last 20 messages from every Kal Discord channel
and flags anything that looks like spam, errors, or duplicates.

Run with:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot\\bot
  python audit_discord.py
"""
from __future__ import annotations

import asyncio
import datetime
import sys
from collections import defaultdict

# ── Problem signatures to flag ────────────────────────────────────────────────

FLAG_PATTERNS = [
    ("Traceback",                "raw traceback"),
    ("Too Many Requests",        "rate-limit error"),
    ("credit balance is too low","credits exhausted"),
    ("Non-fatal error",          "non-fatal error message"),
    ("KXBTC15M",                 "raw ticker string"),
    ("KXETH15M",                 "raw ticker string"),
    ("KXSOL15M",                 "raw ticker string"),
    ("HTTPStatusError",          "raw HTTP error"),
    ("ConnectError",             "connection error"),
    ("ReadTimeout",              "timeout error"),
    ("500 Internal Server",      "server error"),
    ("Something went wrong",     "error embed"),
    ("Non-fatal",                "non-fatal error"),
]

CHANNELS = [
    # 🧠 INTELLIGENCE
    "morning-brief",
    "breaking-news",
    "big-money",
    "thesis",
    # 📊 MARKETS
    "trades",
    "watchlist",
    "weekly-analysis",
    # 📈 ASSET CLASSES
    "crypto",
    "stocks",
    "prediction-markets",
    "commodities",
    # 🚨 SIGNALS
    "high-conviction",
    "intelligence-feed",
    # ⚙️ SYSTEM
    "summary",
    "alerts",
]

DUPLICATE_WINDOW_SECS = 600  # 10 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flag_reasons(content: str) -> list[str]:
    """Return a list of flag reasons for this message content, or empty list."""
    reasons = []
    for pattern, label in FLAG_PATTERNS:
        if pattern.lower() in content.lower():
            reasons.append(label)
    return reasons


def _ts(msg: dict) -> datetime.datetime:
    """Parse Discord snowflake ID → UTC datetime."""
    snowflake = int(msg["id"])
    ts_ms = (snowflake >> 22) + 1420070400000
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000)


def _et_str(dt: datetime.datetime) -> str:
    """Format UTC datetime as Eastern Time string (no pytz needed)."""
    # ET = UTC-5 (EST) / UTC-4 (EDT) — approximate with UTC-5 for simplicity
    et = dt - datetime.timedelta(hours=5)
    return et.strftime("%Y-%m-%d %H:%M ET")


def _find_duplicates(messages: list[dict]) -> set[str]:
    """Return set of message IDs that are duplicates within DUPLICATE_WINDOW_SECS."""
    dupes: set[str] = set()
    for i, msg in enumerate(messages):
        content_i = msg.get("content", "").strip()[:200]
        ts_i = _ts(msg)
        for j in range(i + 1, len(messages)):
            content_j = messages[j].get("content", "").strip()[:200]
            ts_j = _ts(messages[j])
            if content_i and content_i == content_j:
                diff = abs((ts_i - ts_j).total_seconds())
                if diff <= DUPLICATE_WINDOW_SECS:
                    dupes.add(msg["id"])
                    dupes.add(messages[j]["id"])
    return dupes


# ── Per-channel audit ─────────────────────────────────────────────────────────

async def audit_channel(
    bot,
    ch_id: int,
    ch_name: str,
    bot_id: int,
) -> dict:
    """
    Fetch last 20 messages, flag problems, return summary dict.
    """
    try:
        messages = await bot.get_messages(ch_id, limit=20)
    except Exception as exc:
        print(f"\n  #{ch_name}: FETCH FAILED — {exc}")
        return {
            "channel": ch_name,
            "total": 0,
            "flagged": 0,
            "fetch_error": str(exc),
        }

    dupe_ids = _find_duplicates(messages)

    flagged_count = 0
    rows = []

    for msg in messages:
        content   = msg.get("content", "")
        ts        = _ts(msg)
        ts_str    = _et_str(ts)
        preview   = content[:100].replace("\n", " ").strip()
        author_id = int(msg.get("author", {}).get("id", 0))
        is_bot    = (author_id == bot_id)

        reasons = _flag_reasons(content)
        if msg["id"] in dupe_ids:
            reasons.append("duplicate within 10 min")

        if reasons:
            flagged_count += 1

        rows.append({
            "ts":      ts_str,
            "preview": preview,
            "is_bot":  is_bot,
            "reasons": reasons,
            "msg_id":  msg["id"],
        })

    return {
        "channel":  ch_name,
        "total":    len(messages),
        "flagged":  flagged_count,
        "rows":     rows,
    }


# ── Print helpers ─────────────────────────────────────────────────────────────

FLAG_ICON    = "[FLAG]"
OK_ICON      = "     "
BOT_TAG      = "[bot]"
HUMAN_TAG    = "[usr]"

def print_channel_report(result: dict) -> None:
    ch = result["channel"]
    total   = result.get("total", 0)
    flagged = result.get("flagged", 0)

    header_flag = f"  ({flagged} flagged)" if flagged else "  (clean)"
    print(f"\n{'='*66}")
    print(f"  #{ch}   —   {total} messages{header_flag}")
    print(f"{'='*66}")

    if result.get("fetch_error"):
        print(f"  ERROR fetching channel: {result['fetch_error']}")
        return

    for row in result.get("rows", []):
        who   = BOT_TAG if row["is_bot"] else HUMAN_TAG
        icon  = FLAG_ICON if row["reasons"] else OK_ICON
        print(f"  {icon} {who}  {row['ts']}  {row['preview']!r}")
        for reason in row["reasons"]:
            print(f"              !! {reason}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print()
    print("=" * 66)
    print("KAL DISCORD AUDIT")
    print(f"Checking {len(CHANNELS)} channels — last 20 messages each")
    print("=" * 66)

    try:
        from config import settings
    except Exception as exc:
        print(f"[!!] Config failed: {exc}")
        sys.exit(1)

    token = settings.discord_bot_token
    if not token:
        print("[!!] DISCORD_BOT_TOKEN not set — cannot read messages")
        sys.exit(1)

    from discord_bot import DiscordBot
    bot = DiscordBot(token)

    try:
        guild_id = await bot.get_guild_id()
        bot_id   = await bot.get_bot_id()
    except Exception as exc:
        print(f"[!!] Failed to connect to Discord: {exc}")
        sys.exit(1)

    # Build channel name → ID map
    try:
        all_channels = await bot._list_channels(guild_id)
    except Exception as exc:
        print(f"[!!] Failed to list channels: {exc}")
        sys.exit(1)

    ch_map = {
        ch["name"].lower(): int(ch["id"])
        for ch in all_channels
        if ch.get("type") == 0
    }

    results: list[dict] = []

    for ch_name in CHANNELS:
        ch_id = ch_map.get(ch_name.lower())
        if ch_id is None:
            print(f"\n  #{ch_name}: NOT FOUND in server — skipping")
            results.append({"channel": ch_name, "total": 0, "flagged": 0, "not_found": True})
            continue
        result = await audit_channel(bot, ch_id, ch_name, bot_id)
        print_channel_report(result)
        results.append(result)
        await asyncio.sleep(0.5)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_msgs    = sum(r.get("total",   0) for r in results)
    total_flagged = sum(r.get("flagged", 0) for r in results)

    print()
    print("=" * 66)
    print("AUDIT SUMMARY")
    print("=" * 66)
    print(f"  {'Channel':<20}  {'Messages':>8}  {'Flagged':>8}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*8}")
    for r in results:
        flag_str = str(r.get("flagged", 0)) if r.get("flagged") else "-"
        not_found = " (not found)" if r.get("not_found") else ""
        err_str   = " (fetch error)" if r.get("fetch_error") else ""
        print(f"  {r['channel']:<20}  {r.get('total',0):>8}  {flag_str:>8}{not_found}{err_str}")
    print(f"  {'TOTAL':<20}  {total_msgs:>8}  {total_flagged:>8}")

    # Health score
    print()
    if total_flagged == 0:
        score = "CLEAN"
        detail = "No problems detected."
    elif total_flagged <= 3:
        score = "NEEDS ATTENTION"
        detail = f"{total_flagged} flagged message(s) — review above."
    else:
        score = "CRITICAL"
        detail = f"{total_flagged} flagged messages — bot is likely misbehaving."

    print(f"  Overall health:  {score}")
    print(f"  {detail}")
    print()
    print("=" * 66)
    print()


if __name__ == "__main__":
    asyncio.run(main())
