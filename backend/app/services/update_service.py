"""Game and image updates.

Detecting: the installed build comes from the world's Steam app manifest; the latest
build for its branch from Steam app info (steamcmd in a short-lived container, or
api.steamcmd.net), cached for 10 minutes per branch. Image updates compare the local
image digest with the registry's.

Applying a game update uses the image's own updater (docs/game-server-behavior.md): stop the
server process (it saves), back up, then `supervisorctl start enshrouded-updater`, which
runs steamcmd when the build differs and starts the server again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.config import STEAM_APP_ID, Settings
from app.db import session_scope
from app.models import Job, World
from app.services.backup_service import BackupService, cron_trigger
from app.services.docker_adapter import DockerAdapter
from app.services.events import EventLog
from app.services.jobs import JobContext, JobFailed, JobManager
from app.services.world_paths import container_name, installed_build
from app.services.world_service import FIRST_BOOT_TIMEOUT_S, WorldError, WorldService, _now

log = logging.getLogger(__name__)

STEAM_CACHE = timedelta(minutes=10)
UPDATE_JOB_TYPES = ("update", "image_update")
POLICIES = ("manual", "immediate", "when_empty", "window")
IMAGE_POLICIES = ("notify", "when_empty")
API_URL = "https://api.steamcmd.net/v1/info/{app}"

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict[str, Any]:
    """Valve KeyValues text (what `app_info_print` prints) → nested dicts.

    Lines before the first quoted key (steamcmd's banner) are skipped.
    """
    tokens = [m.group(1) if m.group(1) is not None else m.group(2)
              for m in _TOKEN.finditer(text)]
    pos = 0

    def block() -> dict[str, Any]:
        nonlocal pos
        out: dict[str, Any] = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return out
            key = tok
            pos += 1
            if pos >= len(tokens):
                break
            if tokens[pos] == "{":
                pos += 1
                out[key] = block()
            else:
                out[key] = tokens[pos]
                pos += 1
        return out

    return block()


def buildid_from_app_info(text: str, branch: str, app_id: int = STEAM_APP_ID) -> str | None:
    start = text.find(f'"{app_id}"')
    if start < 0:
        return None
    data = parse_vdf(text[start:]).get(str(app_id), {})
    build = data.get("depots", {}).get("branches", {}).get(branch, {}).get("buildid")
    return build if isinstance(build, str) and build.isdigit() else None


@dataclass
class BranchInfo:
    branch: str
    buildid: str | None = None
    checked_at: datetime | None = None
    source: str | None = None
    error: str | None = None


@dataclass
class ImageInfo:
    image: str
    local: str | None = None
    remote: str | None = None
    checked_at: datetime | None = None
    error: str | None = None

    @property
    def update_available(self) -> bool:
        return bool(self.local and self.remote and self.local != self.remote)


@dataclass
class _Window:
    last: datetime = field(default_factory=lambda: datetime.now(UTC))


class UpdateService:
    def __init__(self, settings: Settings, docker: DockerAdapter, worlds: WorldService,
                 jobs: JobManager, events: EventLog, backups: BackupService) -> None:
        self.settings = settings
        self.docker = docker
        self.worlds = worlds
        self.jobs = jobs
        self.events = events
        self.backups = backups
        self._branches: dict[str, BranchInfo] = {}
        self._images: dict[str, ImageInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._windows: dict[str, _Window] = {}

    # -- detecting ----------------------------------------------------------------------

    async def latest_build(self, branch: str, *, force: bool = False) -> BranchInfo:
        info = self._branches.get(branch)
        if not force and info and info.checked_at and _now() - info.checked_at < STEAM_CACHE:
            return info
        lock = self._locks.setdefault(f"branch:{branch}", asyncio.Lock())
        async with lock:
            info = self._branches.get(branch)
            if not force and info and info.checked_at and _now() - info.checked_at < STEAM_CACHE:
                return info
            info = await self._fetch_build(branch)
            self._branches[branch] = info
            return info

    async def _fetch_build(self, branch: str) -> BranchInfo:
        sources = [self.settings.steam_info_source]
        if self.settings.steam_api_fallback and "api" not in sources:
            sources.append("api")
        errors = []
        for source in sources:
            try:
                build = await (self._from_api(branch) if source == "api"
                               else self._from_steamcmd(branch))
            except Exception as e:  # network, Docker, parse
                errors.append(f"{source}: {e}")
                continue
            if build:
                return BranchInfo(branch, build, _now(), source)
            errors.append(f"{source}: branch {branch!r} not found")
        msg = "; ".join(errors)
        log.warning("latest build check failed", extra={"data": {"branch": branch,
                                                                   "error": msg}})
        return BranchInfo(branch, None, _now(), None, msg)

    async def _from_steamcmd(self, branch: str) -> str | None:
        res = await self.docker.run_helper(
            self.settings.steamcmd_image,
            ["+login", "anonymous", "+app_info_update", "1",
             "+app_info_print", str(STEAM_APP_ID), "+quit"])
        if res.exit_code != 0:
            raise RuntimeError(f"steamcmd exited with {res.exit_code}")
        return buildid_from_app_info(res.output, branch)

    async def _from_api(self, branch: str) -> str | None:
        def fetch() -> dict[str, Any]:
            req = urllib.request.Request(API_URL.format(app=STEAM_APP_ID),
                                         headers={"User-Agent": "FlameKeeper"})
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed URL
                return json.loads(resp.read())

        data = await asyncio.to_thread(fetch)
        build = (data.get("data", {}).get(str(STEAM_APP_ID), {}).get("depots", {})
                 .get("branches", {}).get(branch, {}).get("buildid"))
        return str(build) if build else None

    async def image_info(self, image: str, *, force: bool = False) -> ImageInfo:
        info = self._images.get(image)
        ttl = timedelta(hours=self.settings.image_check_interval_hours)
        if not force and info and info.checked_at and _now() - info.checked_at < ttl:
            return info
        info = ImageInfo(image, checked_at=_now())
        try:
            info.local = await self.docker.local_digest(image)
            info.remote = await self.docker.registry_digest(image)
        except Exception as e:
            info.error = str(e)
        self._images[image] = info
        return info

    async def check(self, world: World, *, force: bool = True) -> dict[str, Any]:
        await self.latest_build(world.game_branch, force=force)
        await self.image_info(self.worlds.image(world), force=force)
        return self.status(world)

    # -- status -------------------------------------------------------------------------

    def status(self, world: World) -> dict[str, Any]:
        world = self.worlds.get(world.id)
        installed = installed_build(self.worlds.paths(world))
        branch = self._branches.get(world.game_branch)
        image = self._images.get(self.worlds.image(world))
        latest = branch.buildid if branch else None
        pending = self.pending_job(world)
        return {
            "game": {
                "branch": world.game_branch,
                "installed": installed,
                "latest": latest,
                "checked_at": _iso(branch.checked_at) if branch else None,
                "source": branch.source if branch else None,
                "error": branch.error if branch else None,
                "update_available": bool(installed and latest and installed != latest),
            },
            "image": {
                "image": self.worlds.image(world),
                "local_digest": image.local if image else None,
                "remote_digest": image.remote if image else None,
                "checked_at": _iso(image.checked_at) if image else None,
                "error": image.error if image else None,
                "update_available": bool(image and image.update_available),
            },
            "policy": self.policy(world),
            "pending_job": pending,
            "auto_blocked_build": world.auto_update_failed_build,
        }

    def summary(self, world: World) -> dict[str, Any]:
        """Small part of the status for world cards (from cache, no I/O but the manifest)."""
        installed = installed_build(self.worlds.paths(world))
        branch = self._branches.get(world.game_branch)
        image = self._images.get(self.worlds.image(world))
        latest = branch.buildid if branch else None
        return {
            "latest_build": latest,
            "game_update_available": bool(installed and latest and installed != latest),
            "image_update_available": bool(image and image.update_available),
        }

    def policy(self, world: World) -> dict[str, Any]:
        nxt = None
        if world.update_policy == "window":
            try:
                nxt = cron_trigger(world.update_window_cron).get_next_fire_time(None, _now())
            except WorldError:
                nxt = None
        return {
            "update_policy": world.update_policy,
            "update_window_cron": world.update_window_cron,
            "next_window": _iso(nxt),
            "image_update_policy": world.image_update_policy,
            "pre_update_backup": world.pre_update_backup,
        }

    def set_policy(self, world: World, changes: dict[str, Any]) -> dict[str, Any]:
        if "update_policy" in changes and changes["update_policy"] not in POLICIES:
            raise WorldError(f"Update policy must be one of {', '.join(POLICIES)}", 422)
        if "image_update_policy" in changes and \
                changes["image_update_policy"] not in IMAGE_POLICIES:
            raise WorldError(f"Image policy must be one of {', '.join(IMAGE_POLICIES)}", 422)
        if "update_window_cron" in changes:
            cron_trigger(changes["update_window_cron"])
        fields = {k: v for k, v in changes.items()
                  if k in ("update_policy", "update_window_cron", "image_update_policy",
                           "pre_update_backup")}
        if fields:
            # A changed policy is a fresh start for a build that failed before.
            self.worlds._update(world.id, auto_update_failed_build=None, **fields)
            self._windows.pop(world.id, None)
        return self.policy(self.worlds.get(world.id))

    def pending_job(self, world: World) -> dict[str, Any] | None:
        with session_scope() as s:
            job = s.scalar(select(Job).where(Job.world_id == world.id,
                                             Job.type.in_(UPDATE_JOB_TYPES),
                                             Job.status.in_(("queued", "running")))
                           .order_by(Job.created_at.desc()).limit(1))
            if job is None:
                return None
            return {"id": job.id, "type": job.type, "status": job.status,
                    "message": job.message}

    def history(self, world: World, limit: int = 20) -> list[dict[str, Any]]:
        with session_scope() as s:
            jobs = s.scalars(select(Job).where(Job.world_id == world.id,
                                               Job.type.in_(UPDATE_JOB_TYPES))
                             .order_by(Job.created_at.desc()).limit(limit)).all()
            return [{
                "id": j.id, "type": j.type, "status": j.status, "message": j.message,
                "started_at": _iso(j.started_at), "finished_at": _iso(j.finished_at),
                "created_at": _iso(j.created_at),
            } for j in jobs]

    # -- applying -----------------------------------------------------------------------

    def update(self, world: World, *, target: str = "game", when: str = "now",
               force: bool = False, auto: bool = False) -> str:
        if self.pending_job(world):
            raise WorldError("An update for this world is already queued or running", 409)
        if target == "image":
            return self.jobs.submit("image_update", world.id,
                                    lambda ctx: self._image_job(ctx, world, when, auto),
                                    f"Updating the image of {world.display_name}")
        label = "Force-updating" if force else "Updating"
        return self.jobs.submit("update", world.id,
                                lambda ctx: self._game_job(ctx, world, when, force, auto),
                                f"{label} {world.display_name}")

    async def _running(self, world: World) -> bool:
        info = await self.docker.get_container(container_name(world.slug))
        return bool(info and info.running)

    async def _game_job(self, ctx: JobContext, world: World, when: str, force: bool,
                        auto: bool) -> None:
        current = self.worlds.get(world.id)
        paths = self.worlds.paths(current)
        name = container_name(current.slug)
        target = (await self.latest_build(current.game_branch)).buildid
        before = installed_build(paths)
        try:
            was_running = await self._running(current)
            if was_running:
                if when == "empty":
                    await self.worlds._wait_until_empty(ctx, current)
                ctx.progress(2, "Stopping the server (it saves first)")
                await self.worlds._supervisorctl(ctx, name, "stop")
            await self._pre_update_backup(ctx, current, "pre-update",
                                          f"Before update {before or '?'} → {target or 'latest'}")
            if was_running:
                self.worlds._apply_config(ctx, current)
                program = "enshrouded-force-update" if force else "enshrouded-updater"
                if self.worlds.monitor:
                    self.worlds.monitor.expect_restart(current.id)
                since = _now()
                ctx.progress(5, "Checking Steam and downloading the update")
                await self.worlds._supervisorctl(ctx, name, "start", program=program)
                self.worlds._mark_applied(current)
                await self.worlds._wait_online(ctx, current, FIRST_BOOT_TIMEOUT_S,
                                               span=(5, 99), since=since)
            else:
                if force:
                    paths_manifest = paths.server / "steamapps"
                    ctx.log(f"Force update: removing {paths_manifest}")
                    await asyncio.to_thread(shutil.rmtree, paths_manifest, True)
                # A stopped world: its container's start runs the image's updater first.
                ctx.log("The world is stopped: starting it updates the game")
                await self.worlds.start_in_job(ctx, current, span=(5, 99),
                                               timeout_s=FIRST_BOOT_TIMEOUT_S)
            after = installed_build(paths)
            if not force and target and after != target:
                raise JobFailed(f"The build is still {after or 'unknown'}, expected {target}",
                                "Try 'Force update', which re-downloads the game files.")
            if not was_running:
                ctx.log("Stopping the world again, as it was before the update")
                await self.worlds.stop_in_job(ctx, current)
        except JobFailed:
            if auto and target:
                self.worlds._update(current.id, auto_update_failed_build=target)
            raise
        if auto:
            self.worlds._update(current.id, auto_update_failed_build=None)
        changed = before != after
        self.events.record(current.id, "updated", target="game", from_build=before,
                           to_build=after, forced=force)
        ctx.progress(100, f"Updated {before} → {after}" if changed else
                     f"Already up to date ({after})")

    async def _pre_update_backup(self, ctx: JobContext, world: World, kind: str,
                                 note: str) -> None:
        paths = self.worlds.paths(world)
        if world.pre_update_backup and paths.savegame.is_dir() and any(paths.savegame.iterdir()):
            ctx.progress(4, "Backing up before the update")
            await self.backups.create_in_job(ctx, world, kind=kind, note=note)

    async def _image_job(self, ctx: JobContext, world: World, when: str, auto: bool) -> None:
        current = self.worlds.get(world.id)
        image = self.worlds.image(current)
        before = await self.docker.local_digest(image)
        await self.worlds._ensure_image(ctx, current, pull=True, span=(2, 30))
        after = await self.docker.local_digest(image)
        self._images.pop(image, None)
        running = await self._running(current)
        if running and when == "empty":
            await self.worlds._wait_until_empty(ctx, current)
        if running:
            await self.worlds.stop_in_job(ctx, current)
        await self._pre_update_backup(ctx, current, "pre-image-update",
                                      f"Before updating {image}")
        await self.worlds._prepare_ownership(ctx, current)
        self.worlds._apply_config(ctx, current)
        await self.worlds._create_container(ctx, current)
        if running or current.desired_state == "running":
            ctx.progress(40, "Starting the server on the new image")
            await self.worlds.start_in_job(ctx, current, span=(40, 99))
        self.events.record(current.id, "updated", target="image", image=image,
                           from_digest=before, to_digest=after)
        ctx.progress(100, "Image updated" if before != after else "Image was already current")

    # -- automatic ----------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Refresh stale checks, then start updates the policies call for."""
        started: list[str] = []
        interval = timedelta(minutes=self.settings.update_check_interval_min)
        worlds = self.worlds.list()
        for branch in {w.game_branch for w in worlds}:
            info = self._branches.get(branch)
            if not info or not info.checked_at or _now() - info.checked_at >= interval:
                await self.latest_build(branch, force=True)
        for image in {self.worlds.image(w) for w in worlds}:
            await self.image_info(image)
        for world in worlds:
            try:
                job = await self._auto(world)
            except Exception:
                log.exception("automatic update failed to start for %s", world.slug)
                continue
            if job:
                started.append(job)
        return started

    async def _auto(self, world: World) -> str | None:
        if self.pending_job(world) or not await self._running(world):
            return None  # stopped worlds update when they next start (the image does it)
        st = self.status(world)
        game, image = st["game"], st["image"]
        if game["update_available"] and world.update_policy != "manual" \
                and game["latest"] != world.auto_update_failed_build:
            when = None
            if world.update_policy == "immediate":
                when = "now"
            elif world.update_policy == "when_empty":
                when = "empty"
            elif world.update_policy == "window" and self._window_due(world):
                when = "now"
            if when:
                self.events.record(world.id, "update_available", build=game["latest"],
                                   policy=world.update_policy)
                return self.update(world, when=when, auto=True)
        if image["update_available"] and world.image_update_policy == "when_empty":
            return self.update(world, target="image", when="empty", auto=True)
        return None

    def _window_due(self, world: World) -> bool:
        w = self._windows.setdefault(world.id, _Window())
        nxt = cron_trigger(world.update_window_cron).get_next_fire_time(None, w.last)
        if nxt and nxt <= _now():
            w.last = _now()
            return True
        return False


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
