"""
setup_railway.py — Automatically pushes every environment variable from
bot/.env to your Railway project, then triggers a deployment.

Prerequisites (do these once before running):
  1. railway login          (opens browser — run in a normal terminal)
  2. railway link           (connects this folder to your Railway project)

Then run:
  cd C:\\Users\\andre\\Desktop\\kalshi-bot
  python setup_railway.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BOT_DIR  = Path(__file__).parent / "bot"
ENV_FILE = BOT_DIR / ".env"
PEM_FILE = BOT_DIR / "kalshi_private_key.pem"

# These are file paths or Gmail OAuth2 — meaningless on Railway, skip them
SKIP_VARS = {
    "KALSHI_PRIVATE_KEY_PATH",
    "NEWSLETTER_EMAIL",
    "GMAIL_CREDENTIALS_PATH",
    "GMAIL_TOKEN_PATH",
}

# These override whatever is in .env — Railway runs paper mode always
RAILWAY_OVERRIDES = {
    "PAPER_TRADING":  "true",
    "DEMO_MODE":      "true",
    "RESEARCH_MODE":  "false",
    "KALSHI_DEMO":    "true",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file. Ignores comments and blanks."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip()
        # Strip surrounding quotes if present
        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        if key:
            env[key] = value
    return env


def pem_to_single_line(path: Path) -> str:
    """Convert a multi-line PEM file to one line with literal \\n separators."""
    content = path.read_text(encoding="utf-8").strip()
    return r"\n".join(line for line in content.splitlines())


def runcmd(args: list[str]) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    # shell=True on Windows so npm-installed CLIs are found in PATH
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        shell=(sys.platform == "win32"),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def set_var(key: str, value: str) -> bool:
    """Set a single Railway variable. Returns True on success."""
    # Pass as KEY=VALUE in one argument to avoid shell splitting issues
    rc, out, err = runcmd(["railway", "variables", "set", f"{key}={value}"])
    if rc == 0:
        print(f"  [OK]  {key}")
        return True
    else:
        print(f"  [!!]  {key}  —  {err or out}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("=" * 60)
    print("RAILWAY VARIABLE SETUP")
    print("=" * 60)

    # Sanity checks
    if not ENV_FILE.exists():
        print(f"[!!] .env not found at {ENV_FILE}")
        sys.exit(1)

    rc, out, err = runcmd(["railway", "--version"])
    if rc != 0:
        print("[!!] Railway CLI not found. Run: npm install -g @railway/cli")
        sys.exit(1)
    print(f"[OK] Railway CLI: {out}")

    # Check we're logged in
    rc, out, err = runcmd(["railway", "whoami"])
    if rc != 0:
        print("[!!] Not logged in. Run: railway login")
        sys.exit(1)
    print(f"[OK] Logged in as: {out}")

    # Check we're linked to a project
    rc, out, err = runcmd(["railway", "status"])
    if rc != 0 or "Project" not in out:
        print()
        print("[!!] Not linked to a Railway project.")
        print("     Run: railway link")
        print("     Then pick your kalshi-bot project from the list.")
        sys.exit(1)
    print(f"[OK] Linked project:")
    for line in out.splitlines():
        print(f"     {line}")

    # Parse .env
    env = read_env(ENV_FILE)
    print(f"\n[OK] Loaded {len(env)} variables from .env")

    # Apply Railway overrides
    env.update(RAILWAY_OVERRIDES)

    # Remove skipped vars
    for skip in SKIP_VARS:
        env.pop(skip, None)

    # Add KALSHI_PRIVATE_KEY from the .pem file
    if PEM_FILE.exists():
        env["KALSHI_PRIVATE_KEY"] = pem_to_single_line(PEM_FILE)
        print(f"[OK] Converted {PEM_FILE.name} → KALSHI_PRIVATE_KEY (single line)")
    else:
        print(f"[!!] {PEM_FILE} not found — KALSHI_PRIVATE_KEY will not be set")

    # Sort for readable output
    keys_sorted = sorted(env.keys())

    print(f"\nSetting {len(keys_sorted)} variables on Railway...\n")

    failed: list[str] = []
    for key in keys_sorted:
        value = env[key]
        if not set_var(key, value):
            failed.append(key)

    # Summary
    print()
    print("=" * 60)
    succeeded = len(keys_sorted) - len(failed)
    print(f"  Set {succeeded}/{len(keys_sorted)} variables successfully")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
        print("  Re-run the script or set them manually in the Railway dashboard.")
    print("=" * 60)

    if failed:
        print()
        sys.exit(1)

    # Trigger deployment
    print()
    print("Triggering deployment...")
    rc, out, err = runcmd(["railway", "up", "--detach"])
    if rc == 0:
        print(f"[OK] Deployment triggered: {out}")
        print()
        print("Watch live logs with:")
        print("  railway logs")
        print()
        print("Or open the dashboard:")
        rc2, url, _ = runcmd(["railway", "open"])
    else:
        print(f"[!!] Deploy trigger failed: {err or out}")
        print("     Go to railway.app and click Deploy manually.")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
