"""Discord notifications: a webhook gets a message for the world events the admin picks.

Messages are queued and sent one at a time by a background task, so recording an event
never waits on Discord. A 429 from Discord is retried after the delay it asks for; other
failures are logged and dropped.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.db import session_scope
from app.models import AppSetting

log = logging.getLogger(__name__)

SETTING_KEY = "notifications"
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
MIN_GAP_S = 0.5  # Discord allows about 5 messages per 2 s per webhook
MAX_ATTEMPTS = 3
QUEUE_MAX = 200

# id -> (label, on by default)
EVENTS: dict[str, tuple[str, bool]] = {
    "online": ("Server online", True),
    "stopped": ("Server stopped", False),
    "crashed": ("Server crashed", True),
    "warning": ("Warnings (e.g. server not answering)", True),
    "player_joined": ("Player joined", True),
    "player_left": ("Player left", False),
    "update_available": ("Game update available", True),
    "updated": ("Update installed", True),
    "restored": ("Backup restored", True),
    "job_failed": ("Something failed (backup, update, restore, start…)", True),
}

GREEN, RED, ORANGE, BLUE, GREY = 0x4FBF8F, 0xE2574C, 0xE3A33B, 0x5AA7D8, 0x9B9BB5
JOB_LABELS = {
    "backup": "Backup", "restore": "Restore", "update": "Game update",
    "image_update": "Image update", "start": "Start", "stop": "Stop",
    "restart": "Restart", "recreate": "Recreate", "create": "Create", "clone": "Clone",
    "delete": "Delete",
}

Post = Callable[[str, dict[str, Any]], Awaitable[httpx.Response]]


class NotificationError(ValueError):
    pass


def mask(url: str) -> str:
    """Show which webhook it is without revealing its token."""
    m = re.match(r"^(https://[^/]+/api/webhooks/\d+)/", url)
    return f"{m.group(1)}/••••••" if m else "••••••"


def embed(title: str, color: int, description: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"title": title[:256], "color": color,
                           "timestamp": datetime.now(UTC).isoformat(),
                           "footer": {"text": "FlameKeeper"}}
    if description:
        out["description"] = description[:4000]
    return out


def event_message(kind: str, world: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """The Discord embed for a world event, or None for kinds we don't send."""
    if kind == "online":
        return embed(f"🟢 {world} is online", GREEN)
    if kind == "stopped":
        return embed(f"⏹ {world} stopped", GREY)
    if kind == "crashed":
        return embed(f"🔴 {world} crashed", RED,
                     "FlameKeeper noticed the server stopped without being asked to.")
    if kind == "warning":
        return embed(f"⚠ {world}", ORANGE, str(data.get("message", "")))
    if kind == "player_joined":
        return embed(f"👋 {data.get('player', 'Someone')} joined {world}", BLUE)
    if kind == "player_left":
        return embed(f"{data.get('player', 'Someone')} left {world}", GREY)
    if kind == "update_available":
        return embed(f"⬆ Game update available for {world}", BLUE,
                     f"Build {data.get('build', '?')}")
    if kind == "updated":
        if data.get("target") == "image":
            return embed(f"✅ {world}: container image updated", GREEN, str(data.get("image", "")))
        before, after = data.get("from_build"), data.get("to_build")
        detail = f"Build {after}" if before == after else f"Build {before} → {after}"
        return embed(f"✅ {world} updated", GREEN, detail)
    if kind == "restored":
        return embed(f"♻ {world} restored from a backup", BLUE, str(data.get("filename", "")))
    return None


def job_failed_message(job: dict[str, Any], world: str) -> dict[str, Any]:
    label = JOB_LABELS.get(job.get("type", ""), str(job.get("type", "Action")).title())
    return embed(f"❌ {label} failed on {world}", RED, str(job.get("message", "")))


async def _httpx_post(url: str, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(url, json=payload)


class Notifier:
    def __init__(self, world_name: Callable[[str], str | None], post: Post = _httpx_post) -> None:
        self.world_name = world_name
        self.post = post
        self._config: dict[str, Any] = {"discord_webhook": "", "events": self._defaults()}
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _defaults() -> list[str]:
        return [k for k, (_, on) in EVENTS.items() if on]

    # -- settings -------------------------------------------------------------------

    def load(self) -> None:
        with session_scope() as s:
            row = s.get(AppSetting, SETTING_KEY)
            if row and isinstance(row.value, dict):
                events = [e for e in row.value.get("events", []) if e in EVENTS]
                self._config = {"discord_webhook": str(row.value.get("discord_webhook", "")),
                                "events": events}

    def view(self) -> dict[str, Any]:
        url = self._config["discord_webhook"]
        return {"discord_configured": bool(url),
                "discord_webhook": mask(url) if url else "",
                "events": list(self._config["events"]),
                "available": [{"id": k, "label": label, "default": on}
                              for k, (label, on) in EVENTS.items()]}

    def update(self, *, discord_webhook: str | None = None,
               events: list[str] | None = None) -> dict[str, Any]:
        """`discord_webhook`: None keeps the current one, "" removes it."""
        config = dict(self._config)
        if discord_webhook is not None:
            url = discord_webhook.strip()
            if url and not WEBHOOK_RE.match(url):
                raise NotificationError(
                    "That isn't a Discord webhook URL. In Discord: Server Settings → "
                    "Integrations → Webhooks → Copy Webhook URL.")
            config["discord_webhook"] = url
        if events is not None:
            unknown = [e for e in events if e not in EVENTS]
            if unknown:
                raise NotificationError(f"Unknown events: {', '.join(unknown)}")
            config["events"] = [e for e in EVENTS if e in events]
        with session_scope() as s:
            row = s.get(AppSetting, SETTING_KEY)
            if row:
                row.value = config
            else:
                s.add(AppSetting(key=SETTING_KEY, value=config))
        self._config = config
        return self.view()

    # -- sending --------------------------------------------------------------------

    def start(self) -> None:
        self._queue = asyncio.Queue(QUEUE_MAX)
        self._task = asyncio.create_task(self._worker(), name="notifications")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        if kind not in self._config["events"]:
            return
        message = event_message(kind, self._name(event.get("world_id")), event.get("data") or {})
        if message:
            self._enqueue(message)

    def on_job(self, job: dict[str, Any]) -> None:
        if job.get("status") != "failed" or "job_failed" not in self._config["events"]:
            return
        self._enqueue(job_failed_message(job, self._name(job.get("world_id"))))

    def _name(self, world_id: str | None) -> str:
        return (self.world_name(world_id) if world_id else None) or "FlameKeeper"

    def _enqueue(self, message: dict[str, Any]) -> None:
        if not self._config["discord_webhook"] or self._queue is None:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("notification queue full; dropping a Discord message")

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            url = self._config["discord_webhook"]
            if url:
                try:
                    await self._send(url, message)
                except Exception as e:
                    log.warning("Discord notification failed: %s", e)
            await asyncio.sleep(MIN_GAP_S)

    async def _send(self, url: str, message: dict[str, Any]) -> None:
        payload = {"username": "FlameKeeper", "embeds": [message],
                   "allowed_mentions": {"parse": []}}
        for _ in range(MAX_ATTEMPTS):
            res = await self.post(url, payload)
            if res.status_code == 429:
                try:
                    wait = float(res.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(min(wait, 30))
                continue
            if res.status_code >= 400:
                raise NotificationError(f"Discord answered {res.status_code}: {res.text[:200]}")
            return
        raise NotificationError("Discord kept asking to slow down")

    async def test(self, discord_webhook: str | None = None) -> None:
        """Send a test message now (to a new URL, or the saved one)."""
        url = (discord_webhook or "").strip() or self._config["discord_webhook"]
        if not url:
            raise NotificationError("Add a Discord webhook URL first.")
        if not WEBHOOK_RE.match(url):
            raise NotificationError("That isn't a Discord webhook URL.")
        try:
            await self._send(url, embed("🔥 FlameKeeper test message", GREEN,
                                        "Notifications from FlameKeeper will show up here."))
        except httpx.HTTPError as e:
            raise NotificationError(f"Couldn't reach Discord: {e}") from e
