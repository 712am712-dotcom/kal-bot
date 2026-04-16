"""
check_startup.py [-] Pre-flight check for Kal.

Run this before starting the bot to verify:
  1. All required env vars are loaded
  2. Discord channels are reachable
  3. Gmail credentials exist (if configured)
  4. Lists all background tasks that will run

Usage:
  cd bot
  python check_startup.py
"""
import asyncio
import os
import sys
from pathlib import Path


def _mask(val: str, show: int = 6) -> str:
    """Show first N chars of a secret, then ****"""
    if not val:
        return "(not set)"
    return val[:show] + "****"


async def main() -> None:
    print("\n" + "=" * 60)
    print("KAL STARTUP CHECK")
    print("=" * 60)

    # ── 1. Config loading ────────────────────────────────────────
    print("\n[1/4] ENVIRONMENT VARIABLES\n")
    try:
        from config import settings
    except Exception as exc:
        print(f"  FATAL: config failed to load: {exc}")
        sys.exit(1)

    checks = [
        ("ANTHROPIC_API_KEY",      settings.anthropic_api_key,       True),
        ("KALSHI_API_KEY",         settings.kalshi_api_key,           True),
        ("SUPABASE_URL",           settings.supabase_url,             True),
        ("SUPABASE_SERVICE_ROLE_KEY", settings.supabase_service_role_key, True),
        ("DISCORD_BOT_TOKEN",      settings.discord_bot_token,        False),
        ("DISCORD_WEBHOOK_URL",    settings.discord_webhook_url,      False),
        ("FINNHUB_API_KEY",        settings.finnhub_api_key,          False),
        ("ALPHA_VANTAGE_KEY",      settings.alpha_vantage_key,        False),
        ("NEWSLETTER_EMAIL",       settings.newsletter_email,         False),
        ("GMAIL_CREDENTIALS_PATH", settings.gmail_credentials_path,   False),
    ]

    all_required_ok = True
    for name, val, required in checks:
        if required:
            if val:
                print(f"  [OK]  {name}: {_mask(val)}")
            else:
                print(f"  [!!]  {name}: MISSING (required)")
                all_required_ok = False
        else:
            status = "[OK]" if val else "[-]"
            display = _mask(val) if val else "(not set [-] feature disabled)"
            print(f"  {status}  {name}: {display}")

    # Gmail credentials file check [-] resolve relative to bot/ dir, not cwd
    _bot_dir   = Path(__file__).parent
    raw_path   = settings.gmail_credentials_path
    creds_path = Path(raw_path) if Path(raw_path).is_absolute() else _bot_dir / Path(raw_path).name
    if settings.newsletter_email:
        if creds_path.exists():
            print(f"\n  [OK]  gmail_credentials.json found at {creds_path}")
        else:
            print(f"\n  [??]  gmail_credentials.json NOT found at {creds_path}")
            print("      Gmail morning brief will be disabled until credentials are set up.")
            print("      See the Gmail OAuth2 setup walkthrough to create this file.")
    else:
        print(f"\n  [-]  NEWSLETTER_EMAIL not set [-] Gmail morning brief disabled")

    if not all_required_ok:
        print("\n  [!!] Some required vars are missing. Check bot/.env")
        sys.exit(1)

    # ── 2. Mode summary ──────────────────────────────────────────
    print(f"\n[2/4] BOT MODE\n")
    mode = "paper" if settings.paper_trading else ("research" if settings.research_mode else "live")
    print(f"  Mode:          {mode.upper()}")
    print(f"  Demo API:      {settings.demo_mode}")
    print(f"  Volume floor:  ${settings.volume_floor:.0f}")
    print(f"  Active coins:  {settings.active_coins}")
    print(f"  Claude model:  {settings.claude_model}")
    print(f"  Fallback model:{settings.claude_fallback_model}")
    print(f"  Daily limit:   ${settings.daily_cost_limit_dollars:.2f}")

    # ── 3. Background tasks ──────────────────────────────────────
    print(f"\n[3/4] BACKGROUND TASKS (paper + live mode)\n")

    tasks = [
        ("TA candle refresh",       "every 15 min",        "Kraken OHLC -> RSI/MACD/BB/EMAs for BTC/ETH/SOL"),
        ("TA hourly summary",        "every 1 hour",        "Posts technical analysis to #analysis"),
        ("News intelligence",        "every 30 min",        "Finnhub news -> Kalshi market matching -> #intelligence"),
        ("Scheduled analysis",       "polls every 15 min",  "Daily briefing 7am ET + weekly analysis Monday 8am ET"),
        ("Gmail morning brief",      "polls every 10 min",  "6–8am ET window: reads newsletter -> posts to #morning-brief"),
        ("Intelligence scanner",     f"every {settings.intelligence_scan_interval} min", "Price moves, volume spikes -> #intelligence"),
        ("Resolution checker",       "every 5 min",         "Resolves settled trades, posts WIN/LOSS to #trades"),
        ("Periodic summary",         "every 3 hours",       "Posts trade summary to #summary"),
        ("Daily performance report", "every 24 hours",      "XLSX + stats to #summary"),
    ]

    for name, interval, desc in tasks:
        print(f"  [OK]  {name}")
        print(f"       {interval} [-] {desc}")

    # ── 4. Discord test messages ─────────────────────────────────
    print(f"\n[4/4] DISCORD CHANNEL TEST\n")

    import supabase_logger as discord

    # Check if we have any Discord config at all
    has_bot     = bool(settings.discord_bot_token)
    has_webhook = bool(settings.discord_webhook_url or settings.discord_webhook_intelligence)

    if not has_bot and not has_webhook:
        print("  [??]  No Discord token or webhook configured [-] skipping channel tests")
    else:
        print("  Sending test message to #morning-brief...")
        try:
            await discord._send("morning-brief", {
                "content": (
                    "**Kal Startup Check** [-] #morning-brief is working.\n"
                    "Morning briefs will appear here at ~6:30am ET when a newsletter arrives.\n"
                    "_This is a test message from check_startup.py_"
                ),
                "username": "Kal",
            })
            print("  [OK]  #morning-brief OK")
        except Exception as exc:
            print(f"  [!!]  #morning-brief failed: {exc}")

        await asyncio.sleep(0.5)

        print("  Sending test message to #intelligence...")
        try:
            await discord._send("intelligence", {
                "embeds": [{
                    "title": "Kal Startup Check",
                    "description": (
                        "**#intelligence is working.**\n"
                        "This channel will receive:\n"
                        "• Price shift alerts (>10pp move)\n"
                        "• Volume spike alerts (2× in one window)\n"
                        "• News-to-Kalshi market theses\n"
                        "• TODAY'S FOCUS cross-posts from the morning brief\n"
                        "• Economic calendar every morning at 8am ET\n\n"
                        "_This is a test message from check_startup.py_"
                    ),
                    "color": 0x00C076,
                    "footer": {"text": "startup check"},
                }],
                "username": "Kal",
            })
            print("  [OK]  #intelligence OK")
        except Exception as exc:
            print(f"  [!!]  #intelligence failed: {exc}")

    # ── Done ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if all_required_ok:
        print("ALL CHECKS PASSED [-] ready to start")
        print("\nRun the bot with:")
        print("  python main.py --paper")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
