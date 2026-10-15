"""FlameKeeper's own updates: whether a newer release is out, and updating from the UI.

Checking: the latest release comes from the GitHub API (releases/latest of
settings.flamekeeper_repo), every CHECK_EVERY while "Check GitHub for new FlameKeeper
releases" is on, and whenever the admin clicks "Check now".

Updating works for installs made with the installer, whose compose file sets
FLAMEKEEPER_CLI to the host's `flamekeeper` command. A process can't replace the container
it runs in, so the controller starts a helper container from its own image, privileged
and in the host's PID namespace. The helper enters the host (nsenter -t 1) and runs
`flamekeeper update --version X --no-firewall`, exactly what an admin would type: that
downloads the release's installer, which pulls the new image and recreates the
controller. Game containers aren't touched. The output goes to
DATA_DIR/self-update/update.log. Whichever controller is running when the helper exits
records the result; normally that's the new one, which resumes watching at startup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.db import session_scope
from app.models import AppSetting
from app.services.docker_adapter import DockerAdapter, DockerError
from app.services.notifications import Notifier

log = logging.getLogger(__name__)

STATE_KEY = "app_update"
CHECK_EVERY = timedelta(hours=12)
FIRST_CHECK_DELAY_S = 30.0
LOOP_TICK_S = 600.0
CHECK_NOW_GAP_S = 20.0
HELPER_NAME = "flamekeeper-self-update"
HELPER_TIMEOUT = timedelta(minutes=20)
HOST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COMMAND = "sudo flamekeeper update"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

Fetch = Callable[[str], Awaitable[dict[str, Any]]]


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match(v or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


async def github_latest(repo: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "FlameKeeper"}) as c:
        res = await c.get(f"https://api.github.com/repos/{repo}/releases/latest")
        res.raise_for_status()
        return res.json()


def _now() -> datetime:
    return datetime.now(UTC)


class AppUpdateError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class AppUpdater:
    def __init__(self, settings: Settings, docker: DockerAdapter, notifier: Notifier,
                 fetch: Fetch = github_latest, poll_s: float = 5.0) -> None:
        self.settings = settings
        self.docker = docker
        self.notifier = notifier
        self.fetch = fetch
        self.poll_s = poll_s
        self.first_check_delay = FIRST_CHECK_DELAY_S
        # Saved: latest (release), checked_at, check_error, notified (version), run.
        self._state: dict[str, Any] = {}
        self._last_manual = -CHECK_NOW_GAP_S
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def current(self) -> str:
        return self.settings.flamekeeper_version

    @property
    def log_file(self) -> Path:
        return self.settings.data_dir / "self-update" / "update.log"

    # -- state ----------------------------------------------------------------------

    def load(self) -> None:
        with session_scope() as s:
            row = s.get(AppSetting, STATE_KEY)
            self._state = dict(row.value) if row and isinstance(row.value, dict) else {}

    def _save(self) -> None:
        with session_scope() as s:
            row = s.get(AppSetting, STATE_KEY)
            if row:
                row.value = dict(self._state)
            else:
                s.add(AppSetting(key=STATE_KEY, value=dict(self._state)))

    def _can_update(self) -> str | None:
        """Why FlameKeeper can't update itself here, or None if it can."""
        if parse_version(self.current) is None:
            return "This is a development build, which doesn't update itself."
        if not self.settings.flamekeeper_cli:
            return ("FlameKeeper wasn't installed with the installer, so it can't update "
                    "itself. Pull the new image and recreate its container.")
        return None

    def status(self) -> dict[str, Any]:
        latest = self._state.get("latest")
        cur = parse_version(self.current)
        new = parse_version(latest["version"]) if latest else None
        reason = self._can_update()
        return {
            "current": self.current,
            "latest": latest,
            "update_available": bool(cur and new and new > cur),
            "checked_at": self._state.get("checked_at"),
            "check_error": self._state.get("check_error"),
            "checks_enabled": self.settings.check_for_app_updates,
            "can_update": reason is None,
            "cannot_update_reason": reason,
            "command": COMMAND,
            "run": self._state.get("run"),
        }

    # -- checking -------------------------------------------------------------------

    async def check(self, *, manual: bool = False) -> dict[str, Any]:
        if manual:
            if time.monotonic() - self._last_manual < CHECK_NOW_GAP_S:
                return self.status()
            self._last_manual = time.monotonic()
        try:
            data = await self.fetch(self.settings.flamekeeper_repo)
            tag = str(data.get("tag_name", ""))
            if parse_version(tag) is None:
                raise ValueError(f"unexpected release tag {tag!r}")
            self._state["latest"] = {
                "version": tag.removeprefix("v"),
                "name": data.get("name") or tag,
                "url": data.get("html_url"),
                "published_at": data.get("published_at"),
            }
            self._state["check_error"] = None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("FlameKeeper release check failed: %s", e)
            self._state["check_error"] = f"Couldn't check GitHub: {e}"
        self._state["checked_at"] = _now().isoformat()
        self._save()
        status = self.status()
        version = (status["latest"] or {}).get("version")
        if status["update_available"] and self._state.get("notified") != version:
            self._state["notified"] = version
            self._save()
            self.notifier.on_app_event("app_update_available", {
                "version": version, "current": self.current, "url": status["latest"]["url"]})
        return self.status()

    def _check_due(self) -> bool:
        checked = self._state.get("checked_at")
        return not checked or _now() - datetime.fromisoformat(checked) >= CHECK_EVERY

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_check_delay)
        while True:
            if self.settings.check_for_app_updates and self._check_due():
                await self.check()
            await asyncio.sleep(LOOP_TICK_S)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop(), name="app-update-check"))
        if (self._state.get("run") or {}).get("status") == "running":
            self._tasks.append(asyncio.create_task(self._watch(), name="app-update-watch"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    # -- updating -------------------------------------------------------------------

    async def start_update(self, version: str) -> dict[str, Any]:
        status = self.status()
        if not status["can_update"]:
            raise AppUpdateError(status["cannot_update_reason"])
        if (status["run"] or {}).get("status") == "running":
            raise AppUpdateError("An update is already running.")
        latest = (status["latest"] or {}).get("version")
        if not status["update_available"] or latest != version:
            raise AppUpdateError(f"{version} isn't a newer release than {self.current}. "
                                 "Check for updates again.")
        own = await self.docker.get_container(socket.gethostname())
        if own is None:
            raise AppUpdateError("FlameKeeper couldn't find its own container.")
        old = await self.docker.get_container(HELPER_NAME)
        if old is not None:
            if old.running:
                raise AppUpdateError("An update is already running.")
            await self.docker.remove(HELPER_NAME, force=True)

        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text("")
        script = (f"{shlex.quote(self.settings.flamekeeper_cli)} update "
                  f"--version {shlex.quote(version)} --no-firewall "
                  f">{shlex.quote(str(self.log_file))} 2>&1")
        cmd = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
               "/usr/bin/env", "-i", f"PATH={HOST_PATH}", "HOME=/root", "/bin/sh", "-c", script]
        try:
            await self.docker.run_detached(HELPER_NAME, own.image, cmd,
                                           labels={"flamekeeper.role": "self-update"},
                                           privileged=True, pid_host=True)
        except DockerError as e:
            raise AppUpdateError(f"Couldn't start the update: {e}") from e
        self._state["run"] = {"status": "running", "from": self.current, "to": version,
                              "started_at": _now().isoformat(), "finished_at": None,
                              "error": None, "log_tail": None}
        self._save()
        log.info("updating FlameKeeper %s → %s", self.current, version)
        self._tasks.append(asyncio.create_task(self._watch(), name="app-update-watch"))
        return self.status()

    def _log_tail(self, lines: int = 40) -> str:
        try:
            text = self.log_file.read_text(errors="replace")
        except OSError:
            return ""
        # The installer colors its output; the UI shows plain text.
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        return "\n".join(text.rstrip().splitlines()[-lines:])

    async def _watch(self) -> None:
        """Wait for the helper to exit, then record how the update went."""
        run = self._state["run"]
        started = datetime.fromisoformat(run["started_at"])
        while True:
            helper = await self.docker.get_container(HELPER_NAME)
            if helper is None or not helper.running:
                break
            if _now() - started > HELPER_TIMEOUT:
                break
            await asyncio.sleep(self.poll_s)

        tail = self._log_tail()
        if helper is None:
            error = "The update helper disappeared before it finished."
        elif helper.running:
            error = f"The update didn't finish within {HELPER_TIMEOUT.seconds // 60} minutes."
        elif helper.exit_code != 0:
            last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
            error = f"The update failed (exit code {helper.exit_code}). {last}".strip()
        elif self.current != run["to"]:
            error = f"The update finished, but FlameKeeper is still {self.current}."
        else:
            error = None
        if helper is not None:
            try:
                await self.docker.remove(HELPER_NAME, force=True)
            except DockerError as e:
                log.warning("could not remove %s: %s", HELPER_NAME, e)
        self._state["run"] = {**run, "status": "failed" if error else "succeeded",
                              "finished_at": _now().isoformat(), "error": error,
                              "log_tail": tail}
        self._save()
        if error:
            log.error("FlameKeeper update to %s failed: %s", run["to"], error)
        else:
            log.info("FlameKeeper updated %s → %s", run["from"], run["to"])
        self.notifier.on_app_event("app_updated", {"ok": not error, "from": run["from"],
                                                   "to": run["to"], "error": error})
