"""
discord_bot.py — Discord Bot API client for Kal.

Handles everything that requires the bot token (not a webhook):
  - Creating the 'Kal' channel category and the 4 sub-channels
  - Sending pinned guide cards to each channel
  - Updating the bot's username and avatar
  - Sending messages directly to channels by name (preferred over webhooks)

Usage:
    bot = DiscordBot(token)
    await bot.setup(guides)          # run once at startup
    await bot.send("trades", embed)  # send a message
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import struct
import zlib
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://discord.com/api/v10"

# 4-category structure — each tuple is (category_name, [channel_names_in_order])
CATEGORIES = [
    ("📡 DAILY INTELLIGENCE", ["morning-brief", "attention", "breaking", "patterns"]),
    ("🎬 CONTENT ENGINE",     ["content-queue", "content-output", "content-review"]),
    ("📊 FEEDBACK LOOP",      ["performance", "wins", "misses"]),
    ("⚙️ SYSTEM",             ["alerts", "system-logs"]),
]
# No private channels in the new structure
PRIVATE_CHANNELS: set[str] = set()

# Flat list of all channel names (for backward-compat references)
CHANNEL_ORDER = [ch for _, chs in CATEGORIES for ch in chs]


# ── Avatar generation ─────────────────────────────────────────────────────────

def make_kal_avatar() -> bytes:
    """
    Generate the Kal bot avatar: 512×512 green square (#00C076) with 'Kal' in white.
    Uses Pillow when available; falls back to a solid green PNG with no text.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        size  = 512
        green = (0, 192, 118)   # #00C076
        img   = Image.new("RGB", (size, size), color=green)
        draw  = ImageDraw.Draw(img)
        text  = "Kal"

        # Try common bold font names across Windows / Linux / macOS
        font: Any = None
        for name, fsize in [
            ("arialbd.ttf", 230), ("arial.ttf", 230),
            ("DejaVuSans-Bold.ttf", 230), ("DejaVuSans.ttf", 230),
            ("Helvetica.ttc", 230), ("Helvetica.dfont", 230),
        ]:
            try:
                font = ImageFont.truetype(name, fsize)
                break
            except Exception:
                pass
        if font is None:
            font = ImageFont.load_default(size=200)

        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
            text, fill=(255, 255, 255), font=font,
        )

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    except ImportError:
        log.warning("[discord_bot] Pillow not installed — avatar will be a solid green square")

    # Fallback: solid green PNG (no text) built from raw bytes
    return _solid_png(256, 256, r=0, g=192, b=118)


def _solid_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """Create a solid-color RGB PNG using only the stdlib (no Pillow)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Each row: 1 filter byte (0 = None) + width RGB triplets
    row  = bytes([0]) + bytes([r, g, b] * width)
    idat = zlib.compress(row * height, level=6)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


# ── Discord REST helpers ──────────────────────────────────────────────────────

class DiscordBot:
    """Thin async wrapper around the Discord REST API v10."""

    def __init__(self, token: str) -> None:
        self._token   = token
        self._headers = {
            "Authorization": f"Bot {token}",
            "Content-Type":  "application/json",
        }
        self._guild_id:    int | None        = None
        self._bot_id:      int | None        = None
        self._channel_ids: dict[str, int]    = {}

    # ── Raw HTTP (with 429 retry) ──────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """
        Execute one HTTP request against the Discord API.
        On 429 (rate limit): wait for Retry-After (or 30s fallback) then retry once.
        """
        url = f"{BASE_URL}{path}"
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.request(method, url, headers=self._headers, **kwargs)
            if r.status_code == 429:
                retry_after = float(r.headers.get("retry-after", 30))
                wait = max(retry_after, 30)
                log.warning("[discord_bot] 429 on %s %s — waiting %.0fs", method, path, wait)
                await asyncio.sleep(wait)
                continue
            return r
        # Final attempt — let the caller deal with the error
        async with httpx.AsyncClient(timeout=15.0) as c:
            return await c.request(method, url, headers=self._headers, **kwargs)

    async def _get(self, path: str) -> Any:
        r = await self._request("GET", path)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, data: dict) -> Any:
        r = await self._request("POST", path, json=data)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _patch(self, path: str, data: dict) -> Any:
        r = await self._request("PATCH", path, json=data)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _put(self, path: str) -> None:
        r = await self._request("PUT", path)
        if r.status_code not in (200, 204):
            r.raise_for_status()

    async def _delete(self, path: str) -> None:
        r = await self._request("DELETE", path)
        if r.status_code not in (200, 204):
            r.raise_for_status()

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        """Delete a single message. Silently ignores 404 (already gone)."""
        try:
            r = await self._request(
                "DELETE", f"/channels/{channel_id}/messages/{message_id}"
            )
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()
        except Exception as exc:
            log.debug("[discord_bot] delete_message %s: %s", message_id, exc)

    async def bulk_delete(self, channel_id: int, message_ids: list[int]) -> None:
        """
        Delete up to 100 messages at once (must all be < 14 days old).
        Falls back to one-by-one deletion for any that fail bulk delete.
        """
        if not message_ids:
            return
        # Bulk delete requires 2–100 IDs
        chunks = [message_ids[i:i+100] for i in range(0, len(message_ids), 100)]
        for chunk in chunks:
            if len(chunk) >= 2:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as c:
                        r = await c.post(
                            f"{BASE_URL}/channels/{channel_id}/messages/bulk-delete",
                            headers=self._headers,
                            json={"messages": [str(mid) for mid in chunk]},
                        )
                        if r.status_code in (200, 204):
                            await asyncio.sleep(1.0)  # bulk-delete rate limit
                            continue
                except Exception:
                    pass
            # Fall back to individual deletes
            for mid in chunk:
                await self._delete_message(channel_id, mid)
                await asyncio.sleep(0.4)

    async def get_messages(self, channel_id: int, limit: int = 100) -> list[dict]:
        """Fetch recent messages from a channel."""
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{BASE_URL}/channels/{channel_id}/messages",
                headers=self._headers,
                params={"limit": limit},
            )
            r.raise_for_status()
            return r.json()

    # ── Identity ──────────────────────────────────────────────────────────────

    async def get_bot_id(self) -> int:
        if self._bot_id:
            return self._bot_id
        me = await self._get("/users/@me")
        self._bot_id = int(me["id"])
        return self._bot_id

    async def get_guild_id(self) -> int:
        if self._guild_id:
            return self._guild_id
        guilds = await self._get("/users/@me/guilds")
        if not guilds:
            raise RuntimeError(
                "Bot is not in any Discord server. "
                "Invite it via the Developer Portal first."
            )
        self._guild_id = int(guilds[0]["id"])
        log.info("[discord_bot] guild: %s  id=%s", guilds[0].get("name"), self._guild_id)
        return self._guild_id

    # ── Channel management ────────────────────────────────────────────────────

    async def _list_channels(self, guild_id: int) -> list[dict]:
        return await self._get(f"/guilds/{guild_id}/channels")

    async def _find_or_create_category(self, guild_id: int, name: str) -> int:
        for ch in await self._list_channels(guild_id):
            if ch["type"] == 4 and ch["name"].lower() == name.lower():
                log.info("[discord_bot] category '%s' already exists (%s)", name, ch["id"])
                return int(ch["id"])
        result = await self._post(f"/guilds/{guild_id}/channels", {
            "name": name,
            "type": 4,
        })
        log.info("[discord_bot] created category '%s' (%s)", name, result["id"])
        await asyncio.sleep(0.5)
        return int(result["id"])

    async def _can_send(self, channel_id: int) -> bool:
        """Quick access check — try fetching messages. 403 = bot lacks access."""
        try:
            r = await self._request("GET", f"/channels/{channel_id}/messages", params={"limit": 1})
            return r.status_code not in (403, 401)
        except Exception:
            return False

    async def _find_or_create_channel(
        self, guild_id: int, name: str, category_id: int, position: int,
        private: bool = False,
    ) -> int:
        bot_id = await self.get_bot_id()
        candidates: list[int] = []
        for ch in await self._list_channels(guild_id):
            if ch["type"] == 0 and ch["name"].lower() == name.lower():
                candidates.append(int(ch["id"]))

        # For channels with duplicates (can happen when recreating private channels),
        # prefer the one the bot can actually access.
        for ch_id in candidates:
            if await self._can_send(ch_id):
                log.info("[discord_bot] channel #%s exists + accessible (%s)", name, ch_id)
                return ch_id
        if candidates:
            log.warning("[discord_bot] channel #%s exists but bot has no access — creating new one", name)

        payload: dict = {
            "name":      name,
            "type":      0,               # text channel
            "parent_id": str(category_id),
            "position":  position,
        }
        if private:
            # Deny @everyone VIEW_CHANNEL; explicitly grant the bot user access.
            payload["permission_overwrites"] = [
                {"id": str(guild_id), "type": 0, "allow": "0",  "deny": "1024"},
                {"id": str(bot_id),   "type": 1, "allow": str(1024 + 2048 + 8192 + 65536 + 64), "deny": "0"},
            ]
        result = await self._post(f"/guilds/{guild_id}/channels", payload)
        log.info("[discord_bot] created channel #%s (private=%s) (%s)", name, private, result["id"])
        await asyncio.sleep(0.5)
        return int(result["id"])

    # ── Messages & pins ───────────────────────────────────────────────────────

    async def send(self, channel_id: int, payload: dict) -> int:
        """Send a message to a channel. Returns the message ID."""
        result = await self._post(f"/channels/{channel_id}/messages", payload)
        return int(result["id"])

    async def get_pins(self, channel_id: int) -> list[dict]:
        return await self._get(f"/channels/{channel_id}/pins")

    async def pin(self, channel_id: int, message_id: int) -> None:
        await self._put(f"/channels/{channel_id}/pins/{message_id}")

    async def unpin(self, channel_id: int, message_id: int) -> None:
        await self._delete(f"/channels/{channel_id}/pins/{message_id}")

    # ── Profile ───────────────────────────────────────────────────────────────

    async def update_profile(self, username: str, avatar_bytes: bytes | None = None) -> None:
        """Update the bot's display name and avatar."""
        data: dict = {"username": username}
        if avatar_bytes:
            b64 = base64.b64encode(avatar_bytes).decode()
            data["avatar"] = f"data:image/png;base64,{b64}"
        result = await self._patch("/users/@me", data)
        log.info("[discord_bot] profile → username=%s", result.get("username"))

    # ── Channel ID access ─────────────────────────────────────────────────────

    def channel_id(self, name: str) -> int | None:
        return self._channel_ids.get(name)

    # ── Full setup ────────────────────────────────────────────────────────────

    async def _guide_already_posted(
        self, channel_id: int, bot_id: int, guide_fingerprint: str
    ) -> int | None:
        """
        Scan the last 100 messages in a channel.
        Returns the message ID of an existing guide from this bot, or None.
        Uses the first 80 characters of the guide text as a fingerprint.
        """
        try:
            messages = await self.get_messages(channel_id, limit=100)
            for msg in messages:
                if int(msg.get("author", {}).get("id", 0)) != bot_id:
                    continue
                content = msg.get("content", "")
                if content.startswith(guide_fingerprint):
                    return int(msg["id"])
        except Exception as exc:
            log.debug("[discord_bot] guide check failed for %s: %s", channel_id, exc)
        return None

    async def setup(self, guides: dict[str, str]) -> dict[str, int]:
        """
        Idempotent startup routine — safe to call on every restart.

        For each channel:
          1. Find or create the channel under the 'Kal' category
          2. Check if this bot has already posted the guide (by fingerprinting
             the first 80 chars of the guide text against recent messages)
          3. If a guide already exists: skip posting entirely — no duplicate
          4. If no guide exists: post it once, then pin it (best-effort)

        Pin failures are logged silently — never posted to Discord.
        """
        guild_id = await self.get_guild_id()
        bot_id   = await self.get_bot_id()

        channel_ids: dict[str, int] = {}
        position = 0  # global position counter across all categories

        for cat_name, cat_channels in CATEGORIES:
            cat_id = await self._find_or_create_category(guild_id, cat_name)

            for ch_name in cat_channels:
                ch_id = await self._find_or_create_channel(
                    guild_id, ch_name, cat_id, position,
                    private=(ch_name in PRIVATE_CHANNELS),
                )
                channel_ids[ch_name] = ch_id
                position += 1

                guide_text = guides.get(ch_name)
                if not guide_text:
                    continue

                # Use first 80 chars as fingerprint — unique enough, stable across restarts
                fingerprint = guide_text[:80]

                existing_id = await self._guide_already_posted(ch_id, bot_id, fingerprint)
                if existing_id is not None:
                    log.info("[discord_bot] guide already present in #%s — skipping", ch_name)
                    continue

                # No guide found — post it once
                msg_id: int | None = None
                try:
                    await asyncio.sleep(0.3)
                    msg_id = await self.send(ch_id, {"content": guide_text})
                    log.info("[discord_bot] guide posted in #%s", ch_name)
                except Exception as exc:
                    log.warning("[discord_bot] failed to post guide in #%s: %s", ch_name, exc)

                # Pin it (best-effort — permission failure logged silently, never posted)
                if msg_id:
                    await asyncio.sleep(0.6)
                    try:
                        await self.pin(ch_id, msg_id)
                        log.info("[discord_bot] guide pinned in #%s", ch_name)
                    except Exception as exc:
                        log.warning(
                            "[discord_bot] pin failed in #%s (grant Manage Messages to bot role): %s",
                            ch_name, exc,
                        )
                    await asyncio.sleep(0.6)

        self._channel_ids = channel_ids
        return channel_ids
