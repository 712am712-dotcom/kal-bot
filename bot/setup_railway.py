"""
setup_railway.py -- Push every variable from .env to Railway, then deploy.

Prerequisites (run once in a normal terminal before using this script):
  railway login        # opens browser -- click Authorize
  railway link         # from the kalshi-bot repo root -- pick your project

Then run:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot
  python bot/setup_railway.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ---- Paths (script lives inside bot/, so .env and .pem are siblings) ---------

BOT_DIR  = Path(__file__).parent          # bot/
ENV_FILE = BOT_DIR / ".env"
PEM_FILE = BOT_DIR / "kalshi_private_key.pem"

# ---- Variables to skip -------------------------------------------------------
# File paths and Gmail OAuth2 flows that are meaningless on Railway

SKIP_VARS = {
    "KALSHI_PRIVATE_KEY_PATH",   # replaced by KALSHI_PRIVATE_KEY content below
    "KALSHI_DEMO",               # set explicitly in OVERRIDES
    "NEWSLETTER_EMAIL",          # Gmail OAuth2 requires a browser -- skip on Railway
    "GMAIL_CREDENTIALS_PATH",    # same reason
    "GMAIL_TOKEN_PATH",          # same reason
}

# ---- Railway-specific overrides -- always win over .env ----------------------

OVERRIDES = {
    "PAPER_TRADING":  "true",    # always paper mode on Railway
    "DEMO_MODE":      "true",    # always demo -- no real money
    "RESEARCH_MODE":  "false",   # scan + trade, not research-only
    "KALSHI_DEMO":    "true",    # use demo.kalshi.co API
}


# ---- Helpers -----------------------------------------------------------------

def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file -- skips comments and blanks."""
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip()
        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]   # strip surrounding quotes
        if key:
            env[key] = value
    return env


def pem_to_single_line(path: Path) -> str:
    """Read a PEM file and join lines with literal backslash-n."""
    content = path.read_text(encoding="utf-8").strip()
    return r"\n".join(line for line in content.splitlines())


def run(args: list[str]) -> tuple[int, str, str]:
    """Run a subprocess. shell=True on Windows so npm CLIs are found in PATH."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        shell=(sys.platform == "win32"),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def set_all_vars(env: dict[str, str]) -> tuple[list[str], list[str]]:
    """
    Push all variables in ONE Railway CLI call to avoid per-call rate limits.
    Railway triggers a deployment preview after each individual call, which hits
    the rate limit fast. One batched call avoids this entirely.

    Falls back to one-at-a-time only if the batch call fails, to identify
    which specific variable caused the problem.

    Returns (succeeded_keys, failed_keys).
    """
    if not env:
        return [], []

    # One call: railway variables set K1=V1 K2=V2 K3=V3 ...
    args = ["railway", "variables", "set"] + [f"{k}={v}" for k, v in env.items()]
    rc, out, err = run(args)
    keys = list(env.keys())

    if rc == 0:
        return keys, []

    # Batch failed -- fall back to one-at-a-time to surface the bad variable
    print(f"  [warn] Batch call failed ({err or out}), retrying one-by-one...")
    succeeded, failed = [], []
    for key, value in env.items():
        rc2, out2, err2 = run(["railway", "variables", "set", f"{key}={value}"])
        if rc2 == 0:
            succeeded.append(key)
        else:
            msg = (err2 or out2)[:80]
            print(f"  [!!]  {key:<40}  {msg}")
            failed.append(key)
    return succeeded, failed


# ---- Main --------------------------------------------------------------------

def main() -> None:
    print()
    print("=" * 64)
    print("  RAILWAY VARIABLE SETUP -- Kal")
    print("=" * 64)

    # 1. Check .env exists
    if not ENV_FILE.exists():
        print(f"[!!] .env not found at: {ENV_FILE}")
        sys.exit(1)

    # 2. Check Railway CLI is installed
    rc, out, err = run(["railway", "--version"])
    if rc != 0:
        print("[!!] Railway CLI not found.")
        print("     Fix: npm install -g @railway/cli")
        sys.exit(1)
    print(f"[OK] Railway CLI  {out}")

    # 3. Check logged in
    rc, out, err = run(["railway", "whoami"])
    if rc != 0:
        print("[!!] Not logged in to Railway.")
        print("     Fix: open a terminal and run:  railway login")
        sys.exit(1)
    print(f"[OK] Logged in as  {out}")

    # 4. Check linked to a project
    rc, out, err = run(["railway", "status"])
    if rc != 0 or not out:
        print()
        print("[!!] Not linked to a Railway project.")
        print("     Fix: cd to the repo root and run:  railway link")
        print("     Then re-run this script.")
        sys.exit(1)
    print("[OK] Linked project:")
    for line in out.splitlines():
        print(f"       {line}")

    # 5. Build variable dict
    env = read_env(ENV_FILE)
    print(f"\n[OK] Read {len(env)} variables from .env")

    # Remove skipped vars
    for key in SKIP_VARS:
        env.pop(key, None)

    # Apply Railway overrides (always win)
    env.update(OVERRIDES)

    # Convert .pem -> KALSHI_PRIVATE_KEY single-line content
    if PEM_FILE.exists():
        env["KALSHI_PRIVATE_KEY"] = pem_to_single_line(PEM_FILE)
        print(f"[OK] Converted {PEM_FILE.name} -> KALSHI_PRIVATE_KEY (single line)")
    else:
        print(f"[!!] {PEM_FILE.name} not found -- KALSHI_PRIVATE_KEY will NOT be set")
        print(f"     Expected at: {PEM_FILE}")

    # 6. Drop empty values -- Railway rejects KEY= with no value
    empty_keys = [k for k, v in env.items() if not v]
    for k in empty_keys:
        del env[k]
    if empty_keys:
        print(f"[--] Skipping {len(empty_keys)} empty variable(s): {', '.join(sorted(empty_keys))}")

    # Print what we're about to set
    keys = sorted(env.keys())
    print(f"\nSetting {len(keys)} variables in one Railway call...\n")
    for k in keys:
        v = env[k]
        display = v if len(v) <= 40 else v[:12] + "..." + v[-6:]
        print(f"       {k:<40}  {display}")
    print()

    # 7. Set them all
    succeeded, failed = set_all_vars(env)

    print()
    print("=" * 64)
    print(f"  Variables set:  {len(succeeded)}/{len(keys)}")
    if failed:
        print(f"  Failed:         {', '.join(failed)}")
        print()
        print("  Re-run this script, or add the failed ones manually at:")
        print("  railway.app -> your project -> Variables")
        print("=" * 64)
        print()
        sys.exit(1)
    print("  All variables set successfully.")
    print("=" * 64)

    # 8. Trigger deployment
    print()
    print("Triggering deployment...")
    rc, out, err = run(["railway", "up", "--detach"])
    if rc == 0:
        print("[OK] Deployment started.")
        print()
        print("  Watch live logs:   railway logs")
        print("  Open dashboard:    railway open")
    else:
        msg = (out or err)[:200]
        if "deploy" in msg.lower() or "upload" in msg.lower():
            print(f"[OK] Deployment may have started: {msg}")
        else:
            print(f"[!!] Deploy trigger failed: {msg}")
            print()
            print("  Push to GitHub to trigger automatically:")
            print("  git push origin main")

    print()
    print("=" * 64)
    print("  DONE")
    print("=" * 64)
    print()


if __name__ == "__main__":
    main()
