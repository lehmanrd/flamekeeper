"""World CRUD and lifecycle. Every mutating action runs as a Job."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.config import (
    GAME_IMAGE_REPO,
    GAME_MOUNT,
    LABEL_INFRA,
    LABEL_MANAGED,
    LABEL_VERSION,
    LABEL_WORLD_ID,
    SCHEMA_VERSION,
    Settings,
    is_root,
)
from app.db import session_scope
from app.models import World
from app.services.config_service import ConfigService, initial_document, mask_roles
from app.services.config_validation import validate_bind_ip, validate_roles
from app.services.docker_adapter import (
    ContainerSpec,
    DockerAdapter,
    DockerError,
    split_timestamp,
)
from app.services.events import EventLog
from app.services.jobs import JobContext, JobFailed, JobManager
from app.services.log_patterns import load_patterns
from app.services.ports import host_udp_ports_in_use, suggest_port
from app.services.server_readme import README_NAME, check_install
from app.services.world_paths import (
    SLUG_RE,
    WorldPaths,
    container_name,
    installed_build,
    slugify,
)

if TYPE_CHECKING:
    from app.services.monitor import Monitor

Seed = Callable[[JobContext, World], Awaitable[None]]

log = logging.getLogger(__name__)

NTSYNC_DEVICE = "/dev/ntsync"
WAIT_EMPTY_POLL_S = 30
FIRST_BOOT_TIMEOUT_S = 90 * 60
BOOT_TIMEOUT_S = 10 * 60

# World columns that shape the container. Changing any of them needs a recreate.
INFRA_FIELDS = ("image_tag", "query_port", "bind_ip", "puid", "pgid", "game_branch",
                "steamcmd_args", "use_ntsync", "stop_timeout_s", "cpu_limit", "mem_limit_mb")
UPDATE_POLICIES = ("manual", "immediate", "when_empty", "window")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def infra_fingerprint(world: World, ntsync_available: bool) -> str:
    data = {f: getattr(world, f) for f in INFRA_FIELDS}
    data["image_repo"] = world.image_repo
    data["use_ntsync"] = bool(world.use_ntsync and ntsync_available)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(UTC)


class WorldError(Exception):
    """A request that can't be done; maps to a 4xx problem response."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class CreateWorldInput:
    display_name: str
    slug: str | None = None
    slot_count: int = 16
    query_port: int | None = None
    image_tag: str | None = None
    preset: str = "Default"
    puid: int | None = None
    pgid: int | None = None
    # Wizard. Roles carry passwords; None = the four default roles with
    # generated passwords.
    roles: list[dict[str, Any]] | None = None
    game_branch: str | None = None
    update_policy: str | None = None
    start: bool = True
    # Copy game files from this world instead of downloading them (~9 GB).
    copy_game_from: str | None = None
    # A port that is in use on the host now but will be free before the first start
    # (an imported server that the import stops).
    reusable_port: int | None = None


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                pass
    os.chown(path, uid, gid)


class WorldService:
    def __init__(
        self,
        settings: Settings,
        docker: DockerAdapter,
        jobs: JobManager,
        events: EventLog,
        config: ConfigService,
    ) -> None:
        self.settings = settings
        self.docker = docker
        self.jobs = jobs
        self.events = events
        self.config = config
        self.monitor: Monitor | None = None  # set by app wiring
        self.ntsync_available = False
        self.network: str | None = None  # docker network game containers join
        # Set by app wiring: called with each new world row (default schedules).
        self.on_world_created: Callable[[World], None] | None = None

    # -- lookup -------------------------------------------------------------------------

    def get(self, id_or_slug: str) -> World:
        with session_scope() as s:
            world = s.get(World, id_or_slug) or s.scalar(
                select(World).where(World.slug == id_or_slug)
            )
            if world is None:
                raise WorldError(f"World {id_or_slug!r} not found", 404)
            s.expunge(world)
            return world

    def list(self) -> list[World]:
        with session_scope() as s:
            worlds = s.scalars(select(World).order_by(World.created_at)).all()
            for w in worlds:
                s.expunge(w)
            return list(worlds)

    def paths(self, world: World) -> WorldPaths:
        return WorldPaths.for_slug(self.settings.worlds_dir, world.slug)

    def _poke(self, world_id: str) -> None:
        """Ask the monitor to re-read Docker state now instead of at its next tick."""
        if self.monitor:
            self.monitor.notify(world_id)

    def _update(self, world_id: str, **fields: Any) -> World:
        with session_scope() as s:
            world = s.get(World, world_id)
            if world is None:
                raise WorldError("World was deleted", 404)
            for k, v in fields.items():
                setattr(world, k, v)
            s.flush()
            s.expunge(world)
        if self.monitor:
            self.monitor.notify(world_id)
        return world

    # -- ports --------------------------------------------------------------------------

    async def used_ports(self) -> set[int]:
        used = {w.query_port for w in self.list()}
        return used | await host_udp_ports_in_use(self.docker, self.settings)

    async def suggest_port(self) -> int:
        return suggest_port(await self.used_ports(), self.settings.first_query_port)

    # -- container spec -----------------------------------------------------------------

    def infra_fingerprint(self, world: World) -> str:
        return infra_fingerprint(world, self.ntsync_available)

    def image(self, world: World) -> str:
        return f"{world.image_repo}:{world.image_tag}"

    def container_spec(self, world: World) -> ContainerSpec:
        paths = self.paths(world)
        env = {
            "PUID": str(world.puid),
            "PGID": str(world.pgid),
            "GAME_BRANCH": world.game_branch,
            # Used by the image's own A2S checks; the JSON queryPort must match.
            "SERVER_QUERYPORT": str(world.query_port),
        }
        # SERVER_NAME / SLOT_COUNT / chat env vars are deliberately NOT set: the image
        # rewrites the JSON from them on every container start, which would undo edits
        # made in FlameKeeper (docs/game-server-behavior.md).
        if world.steamcmd_args and world.steamcmd_args != "validate":
            env["STEAMCMD_ARGS"] = world.steamcmd_args
        env.update(self.config.container_env(world))
        devices = [NTSYNC_DEVICE] if world.use_ntsync and self.ntsync_available else []
        return ContainerSpec(
            name=container_name(world.slug),
            hostname=container_name(world.slug),
            image=self.image(world),
            labels={
                LABEL_MANAGED: "true",
                LABEL_WORLD_ID: world.id,
                LABEL_VERSION: str(SCHEMA_VERSION),
                LABEL_INFRA: self.infra_fingerprint(world),
            },
            env=env,
            ports={f"{world.query_port}/udp": (world.bind_ip, world.query_port)},
            binds={str(paths.game): GAME_MOUNT},
            devices=devices,
            stop_timeout=world.stop_timeout_s,
            nano_cpus=int(world.cpu_limit * 1e9) if world.cpu_limit else None,
            mem_limit_bytes=world.mem_limit_mb * 1024 * 1024 if world.mem_limit_mb else None,
            network=self.network,
        )

    # -- create -------------------------------------------------------------------------

    async def validate_create(self, data: CreateWorldInput) -> CreateWorldInput:
        name = data.display_name.strip()
        if not name:
            raise WorldError("Display name is required")
        slug = (data.slug or slugify(name)).strip()
        if not SLUG_RE.match(slug):
            raise WorldError("Slug must be 1–40 characters: a-z, 0-9 and '-', "
                             "not starting or ending with '-'")
        if not 1 <= data.slot_count <= 16:
            raise WorldError("Slot count must be between 1 and 16")
        worlds = self.list()
        if any(w.slug == slug for w in worlds):
            raise WorldError(f"A world with slug {slug!r} already exists", 409)
        if WorldPaths.for_slug(self.settings.worlds_dir, slug).root.exists():
            raise WorldError(
                f"Data directory for {slug!r} already exists on disk; choose another slug", 409
            )
        port = data.query_port
        if port is None:
            port = await self.suggest_port()
        elif not 1024 <= port <= 65535:
            raise WorldError("Query port must be between 1024 and 65535")
        elif any(w.query_port == port for w in worlds):
            raise WorldError(f"Port {port} is already used by another world", 409)
        elif port != data.reusable_port and \
                port in await host_udp_ports_in_use(self.docker, self.settings):
            raise WorldError(f"UDP port {port} is already in use on the host", 409)
        if data.roles is not None:
            problems = validate_roles(data.roles, data.slot_count)
            if problems.errors:
                first = problems.errors[0]
                raise WorldError(f"Roles: {first['msg']}", 422)
        branch = data.game_branch or "public"
        if not _BRANCH_RE.match(branch):
            raise WorldError("Steam branch may use letters, digits, '_', '.' and '-'", 422)
        tag = data.image_tag or self.settings.default_image_tag
        if not _TAG_RE.match(tag):
            raise WorldError("Image tag may use letters, digits, '_', '.' and '-'", 422)
        if data.update_policy is not None and data.update_policy not in UPDATE_POLICIES:
            raise WorldError(f"Update policy must be one of {', '.join(UPDATE_POLICIES)}", 422)
        if data.copy_game_from:
            source = next((w for w in worlds if w.id == data.copy_game_from
                           or w.slug == data.copy_game_from), None)
            if source is None:
                raise WorldError("The world to copy game files from doesn't exist", 404)
            if installed_build(self.paths(source)) is None:
                raise WorldError(f"{source.display_name} has no installed game to copy", 409)
        return CreateWorldInput(
            display_name=name,
            slug=slug,
            slot_count=data.slot_count,
            query_port=port,
            image_tag=tag,
            preset=data.preset,
            puid=data.puid if data.puid is not None else self.settings.default_puid,
            pgid=data.pgid if data.pgid is not None else self.settings.default_pgid,
            roles=data.roles,
            game_branch=branch,
            update_policy=data.update_policy,
            start=data.start,
            copy_game_from=data.copy_game_from,
            reusable_port=data.reusable_port,
        )

    async def preview(self, data: CreateWorldInput) -> dict[str, Any]:
        """Dry run of create: validates and returns the config it would write (masked)."""
        data = await self.validate_create(data)
        doc = initial_document(name=data.display_name, query_port=data.query_port,
                               slot_count=data.slot_count, preset=data.preset,
                               roles=data.roles)
        return {"slug": data.slug, "query_port": data.query_port, "image_tag": data.image_tag,
                "game_branch": data.game_branch, "document": mask_roles(doc)}

    async def create(
        self,
        data: CreateWorldInput,
        *,
        seed: Seed | None = None,
        job_type: str = "create",
        copy_game: Seed | None = None,
    ) -> tuple[World, str]:
        """Create a world. `seed` runs after the config is written and before the first
        start (clone uses it to put saves and settings in place). `copy_game` puts game
        files in place instead of copying them from another world (import uses it)."""
        data = await self.validate_create(data)
        with session_scope() as s:
            world = World(
                slug=data.slug,
                display_name=data.display_name,
                image_repo=GAME_IMAGE_REPO,
                image_tag=data.image_tag,
                query_port=data.query_port,
                puid=data.puid,
                pgid=data.pgid,
                game_branch=data.game_branch or "public",
                desired_state="running" if data.start else "stopped",
                config_revision=1,
                applied_revision=1,
            )
            s.add(world)
            s.flush()
            s.expunge(world)
        try:
            doc = initial_document(
                name=data.display_name,
                query_port=data.query_port,
                slot_count=data.slot_count,
                preset=data.preset,
                roles=data.roles,
            )
        except ValueError as e:
            await self._delete_row(world.id)
            raise WorldError(str(e)) from e
        if data.update_policy:
            world = self._update(world.id, update_policy=data.update_policy)
        if self.on_world_created:
            self.on_world_created(world)
        source = next((w for w in self.list() if data.copy_game_from in (w.id, w.slug)),
                      None) if data.copy_game_from else None
        job_id = self.jobs.submit(
            job_type, world.id,
            lambda ctx: self._create_job(ctx, world, doc, seed, start=data.start,
                                         copy_from=source, copy_game=copy_game),
            f"Creating {data.display_name}")
        self.events.record(world.id, "created", slug=world.slug, port=world.query_port)
        return world, job_id

    async def _create_job(self, ctx: JobContext, world: World, doc: dict[str, Any],
                          seed: Seed | None = None, *, start: bool = True,
                          copy_from: World | None = None,
                          copy_game: Seed | None = None) -> None:
        paths = self.paths(world)
        ctx.progress(0, "Preparing directories")
        for d in (paths.server, paths.backups, paths.history):
            d.mkdir(parents=True, exist_ok=True)
        if copy_from is not None:
            await self._copy_game_files(ctx, copy_from, world)
        elif copy_game is not None:
            await copy_game(ctx, world)
        self.config.write_initial(world, doc)
        ctx.log(f"Wrote {paths.config}")
        if seed:
            await seed(ctx, world)
            self._apply_config(ctx, world)
        if is_root():
            _chown_tree(paths.game, world.puid, world.pgid)
            ctx.log(f"Set ownership of game directory to {world.puid}:{world.pgid}")
        else:
            ctx.log(f"Not running as root: game directory left owned by uid {os.getuid()}")
        await self._ensure_image(ctx, world, pull=True, span=(2, 20))
        await self._create_container(ctx, world)
        if not start:
            copied = copy_from is not None or copy_game is not None
            ctx.progress(100, "Created; start it when you're ready" + (
                "" if copied else " (the first start downloads the game)"))
            return
        ctx.progress(20, "Starting server" + (
            " (first start checks the game files)" if copy_from or copy_game
            else " (first start downloads the game)"))
        since = _now()
        await self._start_container(container_name(world.slug))
        self._poke(world.id)
        self.events.record(world.id, "started")
        await self._wait_online(ctx, world, FIRST_BOOT_TIMEOUT_S, span=(20, 99), since=since)
        ctx.progress(100, "World is online")

    async def _copy_game_files(self, ctx: JobContext, source: World, world: World) -> None:
        """Copy another world's installed game (not its saves, logs, backups or config).

        A local copy is much faster than a 9 GB download; the image's updater still
        checks the files against Steam on the first start.
        """
        src = self.paths(source).game
        dest = self.paths(world).game
        skip = {"savegame", "logs", "backups", "enshrouded_server.json"}
        total = sum(f.stat().st_size for f in src.rglob("*") if f.is_file()) or 1
        copied = 0
        ctx.progress(1, f"Copying game files from {source.display_name}")

        def ignore(folder: str, names: list[str]) -> set[str]:
            return skip & set(names) if Path(folder) == src / "server" else set()

        def copy_file(a: str, b: str) -> None:
            nonlocal copied
            shutil.copy2(a, b, follow_symlinks=False)
            copied += os.lstat(a).st_size

        def copy() -> None:
            # symlinks=True keeps links as links: the Proton/Wine prefix under
            # steamapps/compatdata needs its directory symlinks (dosdevices/c: etc.).
            shutil.copytree(src, dest, symlinks=True, ignore=ignore,
                            copy_function=copy_file, dirs_exist_ok=True)

        task = asyncio.create_task(asyncio.to_thread(copy))
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            ctx.progress(1 + int(copied * 18 / total),
                         f"Copying game files from {source.display_name}: "
                         f"{copied * 100 // total}%")
        await task
        ctx.log(f"Copied {copied // 1024**2} MB of game files from {source.slug}")

    async def _delete_row(self, world_id: str) -> None:
        with session_scope() as s:
            world = s.get(World, world_id)
            if world:
                s.delete(world)

    # -- container helpers --------------------------------------------------------------

    async def _ensure_image(
        self, ctx: JobContext, world: World, *, pull: bool, span: tuple[int, int]
    ) -> None:
        image = self.image(world)
        if not pull and await self.docker.image_exists(image):
            return
        lo, hi = span
        ctx.progress(lo, f"Pulling {image}")
        ctx.log(f"Pulling image {image}")

        def on_progress(pct: int | None, msg: str) -> None:
            ctx.progress(lo + int((pct or 0) * (hi - lo) / 100), msg)

        try:
            await self.docker.pull(image, on_progress)
        except DockerError as e:
            if await self.docker.image_exists(image):
                ctx.log(f"Pull failed ({e}); using the local copy of {image}")
                return
            raise JobFailed(f"Could not pull {image}: {e}",
                            "Check the host's internet access and the image tag.") from e
        ctx.log(f"Image {image} ready")

    async def _create_container(self, ctx: JobContext, world: World) -> None:
        name = container_name(world.slug)
        existing = await self.docker.get_container(name)
        if existing:
            if existing.labels.get(LABEL_WORLD_ID) != world.id:
                raise JobFailed(
                    f"A container named {name} exists and is not managed by this world",
                    "Remove or rename that container, then try again.",
                )
            ctx.log(f"Removing old container {name}")
            await self.docker.remove(name, force=True)
        if self.network:
            await self.docker.ensure_network(self.network)
        spec = self.container_spec(world)
        await self.docker.create_container(spec)
        self._poke(world.id)
        ctx.log(f"Created container {name} (image {spec.image}, port {world.query_port}/udp)")

    async def _start_container(self, name: str) -> None:
        """Start a world's container. Its network is created first if it's missing (e.g.
        deleted by hand): Docker accepts a container on a missing network but can't start it."""
        if self.network:
            await self.docker.ensure_network(self.network)
        await self.docker.start(name)

    async def _wait_online(
        self,
        ctx: JobContext,
        world: World,
        timeout_s: float,
        span: tuple[int, int],
        since: datetime,
    ) -> None:
        """Follow the container log until HostOnline, mapping steamcmd progress to the job.

        `since` must be taken just before the start/restart, so that lines from an earlier
        server run (an old HostOnline or failure) are not mistaken for this one.
        """
        patterns = load_patterns()
        name = container_name(world.slug)
        lo, hi = span
        last_pct = -1
        # A steamcmd error the image's updater retries: fatal only if it gives up.
        steam_error: dict[str, Any] | None = None

        async def follow() -> None:
            nonlocal last_pct, steam_error
            # Docker's `since` is whole seconds: ask for a little more, then drop lines
            # older than the action using the exact per-line timestamps.
            async for raw in self.docker.logs_follow(
                name, since=since - timedelta(seconds=1), tail=None, timestamps=True
            ):
                ts, line = split_timestamp(raw)
                if ts is not None and ts < since:
                    continue
                ev = patterns.classify(line)
                if ev is None:
                    continue
                if ev.kind == "host_online":
                    return
                if ev.kind == "steam_progress":
                    pct = int(ev.data["pct"])
                    if pct != last_pct:
                        last_pct = pct
                        done_gb = int(ev.data["done"]) / 1e9
                        total_gb = int(ev.data["total"]) / 1e9
                        ctx.progress(lo + int(pct * (hi - lo - 5) / 100),
                                     f"Downloading game: {pct}% ({done_gb:.1f} / "
                                     f"{total_gb:.1f} GB)")
                elif ev.kind == "steam_update_start":
                    ctx.log("steamcmd: checking for game update")
                elif ev.kind == "steam_success":
                    ctx.log("steamcmd: game files installed")
                    ctx.progress(hi - 5, "Game installed, starting server")
                elif ev.kind == "server_process_start":
                    ctx.log("Server process started")
                elif ev.kind == "steam_retry":
                    ctx.log("steamcmd failed; the image's updater is trying again")
                elif ev.kind == "steam_aborted":
                    title = steam_error["title"] if steam_error else "The game download failed"
                    raise JobFailed(f"{title} (the updater gave up after two tries)",
                                    steam_error["hint"] if steam_error else
                                    "Open the Logs page to see steamcmd's errors.")
                elif ev.kind == "failure" and ev.data.get("retryable"):
                    steam_error = ev.data
                    ctx.log(f"steamcmd: {ev.data['title']}")
                elif ev.kind == "failure":
                    raise JobFailed(ev.data["title"], ev.data["hint"])
            # Stream ended: the container stopped.
            if steam_error:
                raise JobFailed(steam_error["title"], steam_error["hint"])
            raise JobFailed("The game container stopped before the server came online",
                            "Open the Logs page to see why.")

        try:
            await asyncio.wait_for(follow(), timeout_s)
        except TimeoutError as e:
            raise JobFailed(f"Server did not come online within {int(timeout_s / 60)} minutes",
                            "Open the Logs page to see what the server is doing.") from e
        ctx.log("Server is online ('HostOnline')")
        self._check_readme(ctx, world)
        self._poke(world.id)

    # -- lifecycle ------------------------------------------------------------------------

    def _check_readme(self, ctx: JobContext, world: World) -> None:
        """Watch Keen's settings readme for changes the schema doesn't cover yet."""
        paths = self.paths(world)
        try:
            result = check_install(paths.server, paths.root)
        except Exception:  # never fail a lifecycle job over documentation
            log.exception("could not check %s", README_NAME)
            return
        if not result or not result["changed"]:
            return
        ctx.log(f"Game settings readme version {result['version']}: "
                f"{len(result['drift'])} difference(s) from FlameKeeper's settings schema")
        if result["drift"]:
            self.events.record(
                world.id, "warning",
                message=f"The game's settings readme (version {result['version']}) lists "
                        f"{len(result['drift'])} change(s) FlameKeeper doesn't know about yet. "
                        "See Settings → Game settings readme.",
                drift=result["drift"])

    def _apply_config(self, ctx: JobContext, world: World) -> None:
        """Merge saved-but-pending config into the live file. Only call while the server
        process is stopped (see ConfigService.apply_pending)."""
        changes = self.config.apply_pending(world)
        if changes:
            ctx.log("Applied saved config changes: " + ", ".join(c["label"] for c in changes))

    async def apply_config_if_stopped(self, world: World) -> bool:
        """After a save: a stopped world gets the change in its file right away."""
        info = await self.docker.get_container(container_name(world.slug))
        if info is not None and info.running:
            return False
        self.config.apply_pending(world)
        return True

    async def _supervisorctl(self, ctx: JobContext, name: str, action: str,
                             program: str = "enshrouded-server") -> None:
        ctx.log(f"supervisorctl {action} {program}")
        res = await self.docker.exec(name, ["supervisorctl", action, program])
        ctx.log(res.output.strip())
        if res.exit_code != 0:
            raise JobFailed(f"supervisorctl {action} failed", "Try a container restart instead.")

    def _mark_applied(self, world: World) -> None:
        """The server just (re)read the config file: everything saved so far is live."""
        fresh = self.get(world.id)
        if fresh.applied_revision != fresh.config_revision:
            self._update(world.id, applied_revision=fresh.config_revision)
            self.events.record(world.id, "config_applied", revision=fresh.config_revision)

    def recreate_required(self, world: World, labels: dict[str, str]) -> bool:
        """True when the container was created with other infra settings (or before
        FlameKeeper labeled containers with them)."""
        return labels.get(LABEL_INFRA) != self.infra_fingerprint(world)

    async def _prepare_ownership(self, ctx: JobContext, world: World) -> None:
        """After a PUID/PGID change, hand the game directory to the new owner."""
        if not is_root():
            return
        config = self.paths(world).config
        try:
            st = config.stat()
        except FileNotFoundError:
            return
        if (st.st_uid, st.st_gid) != (world.puid, world.pgid):
            ctx.progress(None, f"Changing file ownership to {world.puid}:{world.pgid}")
            await asyncio.to_thread(_chown_tree, self.paths(world).game, world.puid, world.pgid)

    def _submit(self, job_type: str, world: World, fn, message: str) -> str:
        return self.jobs.submit(job_type, world.id, fn, message)

    async def _wait_until_empty(self, ctx: JobContext, world: World) -> None:
        assert self.monitor is not None
        while True:
            count = self.monitor.player_count(world.id)
            if not count:
                return
            ctx.progress(None, f"Waiting for {count} player(s) to leave")
            await asyncio.sleep(WAIT_EMPTY_POLL_S)

    async def start_in_job(self, ctx: JobContext, world: World,
                           span: tuple[int, int] = (30, 99),
                           timeout_s: float = BOOT_TIMEOUT_S) -> None:
        """Start the world (creating or recreating its container if needed) and wait
        until it's online. Shared by start, restore and "stop for a consistent backup"."""
        current = self._update(world.id, desired_state="running")
        name = container_name(world.slug)
        info = await self.docker.get_container(name)
        if info is None:
            ctx.log("Container missing; creating it")
            await self._ensure_image(ctx, current, pull=False, span=(span[0], span[0]))
            await self._create_container(ctx, current)
        elif info.running:
            ctx.log("Already running")
            return
        elif self.recreate_required(current, info.labels):
            ctx.log("Container settings changed; recreating the container first")
            await self._prepare_ownership(ctx, current)
            await self._ensure_image(ctx, current, pull=False, span=(span[0], span[0]))
            await self._create_container(ctx, current)
        self._apply_config(ctx, current)
        if self.monitor:
            self.monitor.expect_restart(world.id)
        since = _now()
        await self._start_container(name)
        self._poke(world.id)
        self.events.record(world.id, "started")
        # The server reads enshrouded_server.json when it boots.
        self._mark_applied(current)
        ctx.progress(span[0], "Waiting for the server to come online")
        await self._wait_online(ctx, current, timeout_s, span=span, since=since)

    async def stop_in_job(self, ctx: JobContext, world: World, *, when: str = "now") -> bool:
        """Stop the world; returns whether it was running. desired_state becomes
        "stopped" so the monitor never takes a deliberate stop for a crash; callers that
        stop only temporarily (restore, consistent backup) start it again afterwards."""
        name = container_name(world.slug)
        info = await self.docker.get_container(name)
        if info is None or not info.running:
            self._update(world.id, desired_state="stopped")
            ctx.log("Already stopped")
            return False
        if when == "empty":
            await self._wait_until_empty(ctx, world)
        self._update(world.id, desired_state="stopped")
        ctx.progress(None, f"Stopping (up to {world.stop_timeout_s}s for a clean save)")
        await self.docker.stop(name, world.stop_timeout_s)
        self._poke(world.id)
        self.events.record(world.id, "stopped")
        return True

    def start(self, world: World) -> str:
        async def run(ctx: JobContext) -> None:
            await self.start_in_job(ctx, world)
            ctx.progress(100, "Online")

        return self._submit("start", world, run, f"Starting {world.display_name}")

    def stop(self, world: World, when: str = "now") -> str:
        async def run(ctx: JobContext) -> None:
            await self.stop_in_job(ctx, world, when=when)
            ctx.progress(None, "Stopped")

        return self._submit("stop", world, run, f"Stopping {world.display_name}")

    def restart(self, world: World, mode: str = "process", when: str = "now") -> str:
        async def run(ctx: JobContext) -> None:
            name = container_name(world.slug)
            info = await self.docker.get_container(name)
            if info is None or not info.running:
                raise JobFailed("The world is not running", "Start it instead.")
            if when == "empty":
                await self._wait_until_empty(ctx, world)
            current = self.get(world.id)
            since = _now()
            # Stop, merge pending config, start: the server never runs while FlameKeeper
            # writes its config, and anything it wrote on the way down (bans) is kept.
            if self.monitor:
                self.monitor.expect_restart(world.id)
            if mode == "process":
                await self._supervisorctl(ctx, name, "stop")
                self._apply_config(ctx, current)
                await self._supervisorctl(ctx, name, "start")
            else:
                ctx.progress(None, f"Stopping (up to {world.stop_timeout_s}s for a clean save)")
                await self.docker.stop(name, world.stop_timeout_s)
                self._apply_config(ctx, current)
                await self._start_container(name)
                self._poke(world.id)
            self.events.record(world.id, "started", mode=mode)
            self._mark_applied(current)
            await self._wait_online(ctx, world, BOOT_TIMEOUT_S, span=(10, 99), since=since)
            ctx.progress(100, "Online")

        return self._submit("restart", world, run, f"Restarting {world.display_name}")

    def recreate(self, world: World) -> str:
        async def run(ctx: JobContext) -> None:
            current = self.get(world.id)
            name = container_name(world.slug)
            info = await self.docker.get_container(name)
            was_running = bool(info and info.running)
            if was_running:
                ctx.progress(5, "Stopping")
                await self.docker.stop(name, current.stop_timeout_s)
                self._poke(world.id)
            await self._prepare_ownership(ctx, current)
            self._apply_config(ctx, current)
            await self._ensure_image(ctx, current, pull=False, span=(10, 30))
            await self._create_container(ctx, current)
            if current.desired_state == "running" or was_running:
                if self.monitor:
                    self.monitor.expect_restart(world.id)
                since = _now()
                await self._start_container(name)
                self._poke(world.id)
                self.events.record(world.id, "started", mode="recreate")
                self._mark_applied(current)
                await self._wait_online(ctx, current, BOOT_TIMEOUT_S, span=(40, 99),
                                        since=since)
            ctx.progress(100, "Recreated")

        return self._submit("recreate", world, run, f"Recreating {world.display_name}")

    # -- infra settings (Server tab) -----------------------------------------------------

    async def update_infra(self, world: World, changes: dict[str, Any]) -> list[str]:
        """Validate and store container settings; returns the fields that changed.

        Nothing is applied to the running container: the response tells the UI a
        recreate is needed. A stopped world picks them up on its next start.
        """
        if self.jobs.is_busy(world.id):
            raise WorldError("Wait for the running job on this world to finish", 409)
        current = self.get(world.id)
        updates = {k: v for k, v in changes.items()
                   if k in INFRA_FIELDS and getattr(current, k) != v}
        await self._validate_infra(current, updates)
        if not updates:
            return []
        self._update(world.id, **updates)
        if "query_port" in updates:
            self.config.set_query_port(current, updates["query_port"])
        log.info("infra settings changed", extra={"data": {"world": world.slug,
                                                           "fields": sorted(updates)}})
        return sorted(updates)

    async def _validate_infra(self, world: World, u: dict[str, Any]) -> None:
        def check(ok: bool, msg: str) -> None:
            if not ok:
                raise WorldError(msg, 422)

        if "image_tag" in u:
            check(isinstance(u["image_tag"], str) and bool(_TAG_RE.match(u["image_tag"])),
                  "Image tag may use letters, digits, '_', '.' and '-' (up to 128)")
        if "game_branch" in u:
            check(isinstance(u["game_branch"], str) and bool(_BRANCH_RE.match(u["game_branch"])),
                  "Steam branch may use letters, digits, '_', '.' and '-'")
        if "steamcmd_args" in u:
            check(isinstance(u["steamcmd_args"], str) and len(u["steamcmd_args"]) <= 200,
                  "steamcmd arguments are limited to 200 characters")
        for key in ("puid", "pgid"):
            if key in u:
                check(isinstance(u[key], int) and 1 <= u[key] <= 2**31 - 1,
                      f"{key.upper()} must be a user/group id of 1 or more")
        if "stop_timeout_s" in u:
            check(isinstance(u["stop_timeout_s"], int) and 10 <= u["stop_timeout_s"] <= 3600,
                  "Stop timeout must be 10–3600 seconds")
        if u.get("cpu_limit") is not None:
            check(0.1 <= u["cpu_limit"] <= 256, "CPU limit must be between 0.1 and 256 CPUs")
        if u.get("mem_limit_mb") is not None:
            check(1024 <= u["mem_limit_mb"] <= 1024 * 1024,
                  "Memory limit must be at least 1024 MB (Keen recommends 16 GB)")
        if "bind_ip" in u:
            msg = validate_bind_ip(u["bind_ip"]) if isinstance(u["bind_ip"], str) else "invalid"
            check(msg is None, f"Bind IP {msg}")
        if "query_port" in u:
            port = u["query_port"]
            check(isinstance(port, int) and 1024 <= port <= 65535,
                  "Query port must be between 1024 and 65535")
            others = [w for w in self.list() if w.id != world.id]
            if any(w.query_port == port for w in others):
                raise WorldError(f"Port {port} is already used by another world", 409)
            if port in await host_udp_ports_in_use(self.docker, self.settings):
                raise WorldError(f"UDP port {port} is already in use on the host", 409)

    def delete(self, world: World, delete_data: bool) -> str:
        async def run(ctx: JobContext) -> None:
            name = container_name(world.slug)
            info = await self.docker.get_container(name)
            if info and info.running:
                ctx.progress(10, "Stopping")
                await self.docker.stop(name, world.stop_timeout_s)
                self._poke(world.id)
            await self.docker.remove(name, force=True)
            ctx.log(f"Removed container {name}")
            if delete_data:
                root = self.paths(world).root
                ctx.log(f"Deleting {root}")
                await asyncio.to_thread(shutil.rmtree, root, ignore_errors=False)
            else:
                ctx.log(f"Kept data in {self.paths(world).root}")
            await self._delete_row(world.id)
            if self.monitor:
                self.monitor.forget(world.id)
            ctx.progress(100, "Deleted")

        return self._submit("delete", world, run, f"Deleting {world.display_name}")

    # -- reconcile ----------------------------------------------------------------------

    async def reconcile(self) -> dict[str, list[str]]:
        """Adopt labeled containers without a DB row; report DB worlds without a container.

        Unlabeled containers are never touched.
        """
        result: dict[str, list[str]] = {"adopted": [], "missing": [], "ok": []}
        worlds = {w.id: w for w in self.list()}
        containers = await self.docker.list_managed()
        seen: set[str] = set()
        for c in containers:
            world_id = c.labels.get(LABEL_WORLD_ID)
            if not world_id:
                continue
            seen.add(world_id)
            if world_id in worlds:
                result["ok"].append(worlds[world_id].slug)
                continue
            adopted = self._adopt(c)
            if adopted:
                result["adopted"].append(adopted.slug)
        for wid, w in worlds.items():
            if wid not in seen:
                result["missing"].append(w.slug)
        if result["adopted"] or result["missing"]:
            log.info("reconcile", extra={"data": result})
        return result

    def _adopt(self, c) -> World | None:
        prefix = container_name("")
        if not c.name.startswith(prefix):
            return None
        slug = c.name[len(prefix):]
        try:
            port = int(c.env.get("SERVER_QUERYPORT", ""))
        except ValueError:
            log.warning("cannot adopt %s: no SERVER_QUERYPORT", c.name)
            return None
        repo, _, tag = c.image.rpartition(":")
        if not repo:
            repo, tag = c.image, "latest"
        paths = WorldPaths.for_slug(self.settings.worlds_dir, slug)
        display_name = slug
        try:
            from app.renderers.json_file import read_config

            doc, _ = read_config(paths.config)
            display_name = doc.get("name") or slug
        except Exception:
            pass
        with session_scope() as s:
            if s.scalar(select(World).where((World.slug == slug) | (World.query_port == port))):
                log.warning("cannot adopt %s: slug or port already used", c.name)
                return None
            world = World(
                id=c.labels[LABEL_WORLD_ID],
                slug=slug,
                display_name=display_name,
                image_repo=repo,
                image_tag=tag,
                query_port=port,
                puid=int(c.env.get("PUID", self.settings.default_puid)),
                pgid=int(c.env.get("PGID", self.settings.default_pgid)),
                game_branch=c.env.get("GAME_BRANCH", "public"),
                desired_state="running" if c.running else "stopped",
                config_revision=1,
                applied_revision=1,
            )
            s.add(world)
            s.flush()
            s.expunge(world)
        self.events.record(world.id, "adopted", container=c.name)
        if self.on_world_created:
            self.on_world_created(world)
        return world
