"""Backups: create, list, restore, upload, retention, clone and schedules.

Zips live in <world>/backups/<timestamp>_<kind>.zip (see backup_archive for the layout).
The Backup table mirrors the folder: zips added or removed by hand are picked up the next
time the list is read. Zips made by the image's own backup program (game/server/backups)
are listed too, as kind "image".
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select
from tzlocal import get_localzone

from app.config import APP_NAME, Settings, is_root
from app.db import session_scope
from app.models import Backup, PlayerSession, Schedule, World
from app.renderers.json_file import dumps_config
from app.services import backup_archive as archive
from app.services.config_service import ConfigService
from app.services.events import EventLog
from app.services.jobs import JobContext, JobFailed, JobManager
from app.services.world_paths import WorldPaths, container_name, installed_build
from app.services.world_service import CreateWorldInput, WorldError, WorldService, _chown_tree

log = logging.getLogger(__name__)

KINDS = ("scheduled", "manual", "pre-update", "pre-restore", "pre-image-update", "uploaded",
         "image")
# Kinds that max_age_days applies to. Scheduled ones follow retention_count; image ones
# belong to the image's own BACKUP_MAX_COUNT.
AGED_KINDS = ("manual", "pre-update", "pre-restore", "pre-image-update", "uploaded")
MIN_INTERVAL = timedelta(minutes=10)
DEFAULT_SCHEDULE = {"cron": "0 * * * *", "enabled": True, "skip_if_idle": True,
                    "retention_count": 48, "max_age_days": 0}
DISK_CACHE_S = 600
SAVE_WAIT_S = 7 * 60
SAVE_POLL_S = 2.0
SAVE_SETTLE_S = 3.0


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def cron_trigger(expr: str) -> CronTrigger:
    """Parse a 5-field cron expression in the controller's time zone (TZ)."""
    try:
        return CronTrigger.from_crontab(expr.strip(), timezone=get_localzone())
    except (ValueError, TypeError) as e:
        raise WorldError(f"Invalid cron expression {expr!r}: {e}", 422) from e


def next_runs(expr: str, after: datetime, count: int = 3) -> list[datetime]:
    trigger = cron_trigger(expr)
    out: list[datetime] = []
    prev = None
    now = after
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, now)
        if nxt is None:
            break
        out.append(nxt)
        prev, now = nxt, nxt + timedelta(seconds=1)
    return out


def validate_cron(expr: str) -> None:
    runs = next_runs(expr, _now(), 4)
    if len(runs) < 2:
        raise WorldError("The schedule never runs (or only once)", 422)
    gaps = [b - a for a, b in zip(runs, runs[1:], strict=False)]
    if min(gaps) < MIN_INTERVAL:
        raise WorldError("Backups can run at most every 10 minutes", 422)


def backup_dict(b: Backup, world: WorldPaths) -> dict[str, Any]:
    return {
        "id": b.id,
        "filename": Path(b.filename).name,
        "path": b.filename,
        "kind": b.kind,
        "note": b.note,
        "size_bytes": b.size_bytes,
        "created_at": _aware(b.created_at).isoformat(),
        "game_build_id": b.game_build_id,
        "saved_at": _aware(b.saved_at).isoformat() if b.saved_at else None,
        "exists": (world.root / b.filename).exists(),
    }


class BackupService:
    def __init__(self, settings: Settings, worlds: WorldService, jobs: JobManager,
                 events: EventLog, config: ConfigService) -> None:
        self.settings = settings
        self.worlds = worlds
        self.jobs = jobs
        self.events = events
        self.config = config
        self._disk_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def paths(self, world: World) -> WorldPaths:
        return self.worlds.paths(world)

    # -- listing ------------------------------------------------------------------------

    def sync(self, world: World) -> None:
        """Make the Backup rows match the zips on disk."""
        paths = self.paths(world)
        found: dict[str, Path] = {}
        for folder in (paths.backups, paths.image_backups):
            if folder.is_dir():
                for p in folder.glob("*.zip"):
                    found[str(p.relative_to(paths.root))] = p
        with session_scope() as s:
            rows = s.scalars(select(Backup).where(Backup.world_id == world.id)).all()
            known = {r.filename for r in rows}
            for r in rows:
                if r.filename not in found:
                    s.delete(r)
            for rel, p in found.items():
                if rel in known:
                    continue
                s.add(self._row_for_file(world, rel, p, image=p.parent == paths.image_backups))

    def _row_for_file(self, world: World, rel: str, path: Path, *, image: bool) -> Backup:
        st = path.stat()
        created = datetime.fromtimestamp(st.st_mtime, UTC)
        kind, note, build = ("image" if image else "uploaded"), "", None
        manifest: dict[str, Any] = {}
        if image:
            note = "Made by the image's backup program"
        else:
            try:
                manifest = archive.inspect_zip(path, trusted=True).manifest or {}
            except archive.ArchiveError:
                manifest = {}
            if manifest.get("kind") in KINDS:
                kind = manifest["kind"]
            note = manifest.get("note") or ""
            build = manifest.get("game_build_id")
            try:
                created = datetime.fromisoformat(manifest["created_at"])
            except (KeyError, TypeError, ValueError):
                pass
        saved = None
        try:
            saved = datetime.fromisoformat(manifest["saved_at"])
        except (KeyError, TypeError, ValueError):
            pass
        return Backup(world_id=world.id, filename=rel, size_bytes=st.st_size,
                      created_at=created, kind=kind, note=note, game_build_id=build,
                      saved_at=saved)

    def list(self, world: World) -> list[dict[str, Any]]:
        self.sync(world)
        paths = self.paths(world)
        with session_scope() as s:
            rows = s.scalars(select(Backup).where(Backup.world_id == world.id)
                             .order_by(Backup.created_at.desc())).all()
            return [backup_dict(b, paths) for b in rows]

    def get(self, world: World, backup_id: str) -> Backup:
        with session_scope() as s:
            b = s.get(Backup, backup_id)
            if b is None or b.world_id != world.id:
                raise WorldError("Backup not found", 404)
            s.expunge(b)
        if not (self.paths(world).root / b.filename).exists():
            self.sync(world)
            raise WorldError("The backup file is gone from disk", 404)
        return b

    def file(self, world: World, b: Backup) -> Path:
        return self.paths(world).root / b.filename

    # -- creating -----------------------------------------------------------------------

    def create(self, world: World, *, kind: str = "manual", note: str = "",
               mode: str = "last_save") -> str:
        """mode (for a running server): "last_save" copies the save on disk as it is;
        "next_save" waits for the server's next autosave first (at most ~5 minutes), so
        recent changes are included; "stop" stops the server, which saves on shutdown,
        and starts it again afterwards."""

        async def run(ctx: JobContext) -> None:
            restart = False
            running = await self._running(world)
            if mode == "stop" and running:
                restart = await self.worlds.stop_in_job(ctx, world)
            elif mode == "next_save" and running:
                await self.wait_for_save(ctx, world)
            try:
                b = await self.create_in_job(ctx, world, kind=kind, note=note)
            finally:
                if restart:
                    ctx.progress(70, "Starting the server again")
                    await self.worlds.start_in_job(ctx, world, span=(70, 99))
            ctx.progress(100, f"Backed up ({b['filename']})")

        label = "Scheduled backup" if kind == "scheduled" else "Backing up"
        return self.jobs.submit("backup", world.id, run, f"{label} {world.display_name}")

    async def _running(self, world: World) -> bool:
        info = await self.worlds.docker.get_container(container_name(world.slug))
        return bool(info and info.running)

    async def wait_for_save(self, ctx: JobContext, world: World,
                            timeout: float = SAVE_WAIT_S) -> None:
        """Wait until the server writes its savegame again (autosave every ~5 minutes)."""
        paths = self.paths(world)
        before = archive.latest_mtime(paths.savegame)
        ctx.progress(None, "Waiting for the server's next save (it saves every 5 minutes)")
        ctx.log(f"Last save on disk: {before.isoformat() if before else 'none'}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(SAVE_POLL_S)
            now = archive.latest_mtime(paths.savegame)
            if now and (before is None or now > before):
                await asyncio.sleep(SAVE_SETTLE_S)  # let the server finish the save
                ctx.log(f"The server saved at {now.isoformat()}")
                return
            if not await self._running(world):
                ctx.log("The server stopped (it saves when it stops)")
                return
        raise JobFailed(f"The server didn't save within {int(timeout / 60)} minutes",
                        "Back up again with 'Stop the server' to force a save.")

    async def create_in_job(self, ctx: JobContext, world: World, *, kind: str,
                            note: str = "") -> dict[str, Any]:
        """Back up the world from inside another job (pre-restore, pre-update, …)."""
        world = self.worlds.get(world.id)
        paths = self.paths(world)
        if not paths.savegame.is_dir() or not any(paths.savegame.iterdir()):
            raise JobFailed("There is no savegame to back up yet",
                            "The server creates it on its first start.")
        ctx.progress(None, "Copying the savegame")
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        dest = paths.backups / f"{stamp}_{kind}.zip"
        n = 2
        while dest.exists():
            dest = paths.backups / f"{stamp}-{n}_{kind}.zip"
            n += 1
        build = installed_build(paths)
        manifest = {
            "app": APP_NAME,
            "app_version": os.environ.get("FLAMEKEEPER_VERSION", "dev"),
            "world": {"id": world.id, "slug": world.slug, "display_name": world.display_name},
            "created_at": _now().isoformat(),
            "kind": kind,
            "note": note,
            "game_build_id": build,
            "config_revision": world.config_revision,
        }

        # The config the world will run with (live file + any pending revision).
        effective = self.config.read(world) if paths.config.exists() else None

        def work() -> tuple[int, datetime | None]:
            with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=paths.root) as tmp:
                staged = Path(tmp) / "savegame"
                archive.snapshot_dir(paths.savegame, staged)  # copy2 keeps mtimes
                saved = archive.latest_mtime(staged)
                manifest["saved_at"] = saved.isoformat() if saved else None
                config = (dumps_config(effective).encode() if effective else None)
                return archive.write_backup(dest, staged, config, manifest), saved

        try:
            size, saved_at = await asyncio.to_thread(work)
        except archive.ArchiveError as e:
            raise JobFailed(str(e), "") from e
        except OSError as e:
            raise JobFailed(f"Could not write the backup: {e}",
                            "Check free disk space and permissions on the data folder.") from e
        with session_scope() as s:
            b = Backup(world_id=world.id, filename=str(dest.relative_to(paths.root)),
                       size_bytes=size, created_at=datetime.fromisoformat(manifest["created_at"]),
                       kind=kind, note=note, game_build_id=build, saved_at=saved_at)
            s.add(b)
            s.flush()
            out = backup_dict(b, paths)
        ctx.log(f"Wrote {dest.name} ({size} bytes)")
        self.events.record(world.id, "backup_created", backup_kind=kind, filename=dest.name,
                           size=size)
        self._disk_cache.pop(world.id, None)
        self.prune(world)
        return out

    # -- editing ------------------------------------------------------------------------

    def set_note(self, world: World, backup_id: str, note: str) -> dict[str, Any]:
        self.get(world, backup_id)
        with session_scope() as s:
            b = s.get(Backup, backup_id)
            b.note = note
            s.flush()
            return backup_dict(b, self.paths(world))

    def delete(self, world: World, backup_id: str) -> None:
        if self.jobs.is_busy(world.id):
            raise WorldError("Wait for the running job on this world to finish", 409)
        b = self.get(world, backup_id)
        self.file(world, b).unlink(missing_ok=True)
        with session_scope() as s:
            row = s.get(Backup, backup_id)
            if row:
                s.delete(row)
        self._disk_cache.pop(world.id, None)

    async def add_upload(self, world: World, tmp: Path, original_name: str) -> dict[str, Any]:
        """Validate an uploaded zip (already saved to `tmp`) and keep it as a backup."""
        try:
            summary = await asyncio.to_thread(archive.inspect_zip, tmp)
        except archive.ArchiveError as e:
            tmp.unlink(missing_ok=True)
            raise WorldError(str(e), 422) from e
        paths = self.paths(world)
        dest = paths.backups / f"{_now().strftime('%Y%m%d-%H%M%S')}_uploaded.zip"
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{dest.stem.split('_')[0]}-{n}_uploaded.zip")
            n += 1
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        manifest = summary.manifest or {}
        note = f"Uploaded {original_name}"
        if manifest.get("world", {}).get("display_name"):
            note += f" (from {manifest['world']['display_name']})"
        with session_scope() as s:
            b = Backup(world_id=world.id, filename=str(dest.relative_to(paths.root)),
                       size_bytes=dest.stat().st_size, created_at=_now(), kind="uploaded",
                       note=note, game_build_id=manifest.get("game_build_id"))
            s.add(b)
            s.flush()
            out = backup_dict(b, paths)
        out["warnings"] = summary.warnings
        out["save_files"] = len(summary.save_files)
        out["has_config"] = summary.has_config
        self._disk_cache.pop(world.id, None)
        return out

    def inspect(self, world: World, backup_id: str) -> dict[str, Any]:
        b = self.get(world, backup_id)
        try:
            summary = archive.inspect_zip(self.file(world, b), trusted=b.kind != "uploaded")
        except archive.ArchiveError as e:
            raise WorldError(str(e), 422) from e
        return {"save_files": summary.save_files, "has_config": summary.has_config,
                "manifest": summary.manifest, "warnings": summary.warnings,
                "uncompressed_bytes": summary.uncompressed_bytes}

    # -- restore ------------------------------------------------------------------------

    def restore(self, world: World, backup_id: str, *, restore_config: bool = False,
                when: str = "now") -> str:
        b = self.get(world, backup_id)
        path = self.file(world, b)
        try:
            summary = archive.inspect_zip(path, trusted=b.kind != "uploaded")
        except archive.ArchiveError as e:
            raise WorldError(f"Can't restore this backup: {e}", 422) from e
        if restore_config and not summary.has_config:
            raise WorldError("This backup has no enshrouded_server.json to restore", 422)

        async def run(ctx: JobContext) -> None:
            current = self.worlds.get(world.id)
            was_running = await self.worlds.stop_in_job(ctx, current, when=when)
            start_after = was_running or current.desired_state == "running"
            paths = self.paths(current)
            if paths.savegame.is_dir() and any(paths.savegame.iterdir()):
                ctx.progress(20, "Safety backup of the current savegame")
                await self.create_in_job(ctx, current, kind="pre-restore",
                                         note=f"Before restoring {path.name}")
            ctx.progress(50, f"Restoring {path.name}")
            try:
                files = await asyncio.to_thread(archive.extract_saves, path, summary,
                                                paths.savegame)
            except (archive.ArchiveError, OSError) as e:
                raise JobFailed(f"Restore failed: {e}", "The previous savegame was kept.") from e
            ctx.log(f"Restored {len(files)} file(s) into {paths.savegame}")
            if is_root():
                await asyncio.to_thread(_chown_tree, paths.savegame, current.puid, current.pgid)
            if restore_config:
                doc = archive.read_config(path) or {}
                result = self.config.apply_document(current, doc, source="backup",
                                                    note=f"From backup {path.name}")
                ctx.log(f"Config restored as revision {result.revision} "
                        f"({len(result.changes)} change(s))")
            self.events.record(current.id, "restored", filename=path.name,
                               config=restore_config)
            if start_after:
                ctx.progress(60, "Starting the server")
                await self.worlds.start_in_job(ctx, current, span=(60, 99))
            ctx.progress(100, "Restored")

        return self.jobs.submit("restore", world.id, run, f"Restoring {world.display_name}")

    # -- clone --------------------------------------------------------------------------

    async def clone(self, world: World, *, source: str, display_name: str,
                    slug: str | None, query_port: int | None) -> tuple[World, str]:
        """New world from this world's current savegame or one of its backups."""
        src = self.worlds.get(world.id)
        src_paths = self.paths(src)
        zip_path: Path | None = None
        summary = None
        if source == "current":
            if not src_paths.savegame.is_dir() or not any(src_paths.savegame.iterdir()):
                raise WorldError("This world has no savegame yet", 409)
            doc = self.config.read(src)
        else:
            b = self.get(src, source)
            zip_path = self.file(src, b)
            try:
                summary = archive.inspect_zip(zip_path, trusted=b.kind != "uploaded")
            except archive.ArchiveError as e:
                raise WorldError(f"Can't clone from this backup: {e}", 422) from e
            doc = archive.read_config(zip_path) or self.config.read(src)
        slot_count = doc.get("slotCount") if isinstance(doc.get("slotCount"), int) else 16

        async def seed(ctx: JobContext, new: World) -> None:
            new_paths = self.paths(new)
            ctx.progress(1, f"Copying the savegame from {src.display_name}")
            if zip_path is not None and summary is not None:
                await asyncio.to_thread(archive.extract_saves, zip_path, summary,
                                        new_paths.savegame)
            else:
                await asyncio.to_thread(archive.snapshot_dir, src_paths.savegame,
                                        new_paths.savegame)
            result = self.config.apply_document(new, doc, source="clone", keep_name=True,
                                                note=f"Cloned from {src.display_name}")
            ctx.log(f"Copied settings and roles from {src.display_name} "
                    f"(revision {result.revision})")

        data = CreateWorldInput(display_name=display_name, slug=slug,
                                slot_count=min(max(slot_count, 1), 16), query_port=query_port,
                                image_tag=src.image_tag, puid=src.puid, pgid=src.pgid)
        return await self.worlds.create(data, seed=seed, job_type="clone")

    # -- retention ----------------------------------------------------------------------

    def prune(self, world: World) -> list[str]:
        """Apply the world's retention: keep N scheduled backups; drop other kinds older
        than max_age_days (when set). Image backups are left to the image."""
        sched = self.schedule_row(world)
        paths = self.paths(world)
        removed: list[str] = []
        with session_scope() as s:
            rows = s.scalars(select(Backup).where(Backup.world_id == world.id)
                             .order_by(Backup.created_at.desc())).all()
            doomed: list[Backup] = []
            if sched.retention_count > 0:
                doomed += [b for b in rows if b.kind == "scheduled"][sched.retention_count:]
            if sched.max_age_days > 0:
                cutoff = _now() - timedelta(days=sched.max_age_days)
                doomed += [b for b in rows
                           if b.kind in AGED_KINDS and _aware(b.created_at) < cutoff]
            for b in doomed:
                (paths.root / b.filename).unlink(missing_ok=True)
                removed.append(Path(b.filename).name)
                s.delete(b)
        if removed:
            log.info("pruned backups", extra={"data": {"world": world.slug, "files": removed}})
            self._disk_cache.pop(world.id, None)
        return removed

    # -- schedules ----------------------------------------------------------------------

    def ensure_default_schedule(self, world: World) -> None:
        with session_scope() as s:
            if s.get(Schedule, (world.id, "backup")) is None:
                # last_run_at = now: the first run is the next cron slot from here.
                s.add(Schedule(world_id=world.id, kind="backup", only_when_empty=False,
                               last_run_at=_now(), **DEFAULT_SCHEDULE))

    def schedule_row(self, world: World) -> Schedule:
        self.ensure_default_schedule(world)
        with session_scope() as s:
            row = s.get(Schedule, (world.id, "backup"))
            s.expunge(row)
            return row

    def schedule(self, world: World) -> dict[str, Any]:
        row = self.schedule_row(world)
        nxt = self.next_run(row) if row.enabled else None
        return {
            "kind": "backup",
            "cron": row.cron,
            "enabled": row.enabled,
            "skip_if_idle": row.skip_if_idle,
            "retention_count": row.retention_count,
            "max_age_days": row.max_age_days,
            "last_run_at": _aware(row.last_run_at).isoformat() if row.last_run_at else None,
            "next_run_at": nxt.isoformat() if nxt else None,
            "upcoming": [d.isoformat() for d in next_runs(row.cron, _now(), 3)],
            "timezone": str(get_localzone()),
        }

    def update_schedule(self, world: World, changes: dict[str, Any]) -> dict[str, Any]:
        if "cron" in changes:
            validate_cron(changes["cron"])
        if not 0 <= changes.get("retention_count", 0) <= 10_000:
            raise WorldError("Keep must be between 0 (all) and 10000", 422)
        if not 0 <= changes.get("max_age_days", 0) <= 36_500:
            raise WorldError("Max age must be between 0 (forever) and 36500 days", 422)
        self.ensure_default_schedule(world)
        with session_scope() as s:
            row = s.get(Schedule, (world.id, "backup"))
            for k in ("cron", "enabled", "skip_if_idle", "retention_count", "max_age_days"):
                if k in changes:
                    setattr(row, k, changes[k].strip() if k == "cron" else changes[k])
            if changes.get("enabled") or "cron" in changes:
                # Count from now: a schedule just turned on doesn't fire for missed slots.
                row.last_run_at = _now()
        self.prune(world)
        return self.schedule(world)

    def next_run(self, row: Schedule) -> datetime | None:
        after = _aware(row.last_run_at) if row.last_run_at else _now()
        runs = next_runs(row.cron, after + timedelta(seconds=1), 1)
        return runs[0] if runs else None

    def played_since(self, world: World, since: datetime | None) -> bool:
        if since is None:
            return True
        with session_scope() as s:
            q = select(PlayerSession.id).where(
                PlayerSession.world_id == world.id,
                or_(PlayerSession.left_at.is_(None), PlayerSession.left_at > since),
            ).limit(1)
            return s.scalar(q) is not None

    def last_backup_at(self, world: World, kind: str) -> datetime | None:
        with session_scope() as s:
            ts = s.scalar(select(Backup.created_at).where(
                Backup.world_id == world.id, Backup.kind == kind)
                .order_by(Backup.created_at.desc()).limit(1))
            return _aware(ts) if ts else None

    def run_due(self, world: World, now: datetime | None = None) -> str | None:
        """Run the scheduled backup if it's due. Returns the job id, or None."""
        now = now or _now()
        row = self.schedule_row(world)
        if not row.enabled:
            return None
        nxt = self.next_run(row)
        if nxt is None or nxt > now:
            return None
        with session_scope() as s:
            s.get(Schedule, (world.id, "backup")).last_run_at = now
        if row.skip_if_idle and not self.played_since(world, self.last_backup_at(world,
                                                                                 "scheduled")):
            log.info("scheduled backup skipped: nobody played since the last one",
                     extra={"data": {"world": world.slug}})
            return None
        paths = self.paths(world)
        if not paths.savegame.is_dir() or not any(paths.savegame.iterdir()):
            return None
        return self.create(world, kind="scheduled")

    # -- disk ---------------------------------------------------------------------------

    def disk(self, world: World, *, refresh: bool = False) -> dict[str, Any]:
        cached = self._disk_cache.get(world.id)
        if cached and not refresh and time.monotonic() - cached[0] < DISK_CACHE_S:
            return cached[1]
        paths = self.paths(world)

        def size(p: Path, exclude: tuple[Path, ...] = ()) -> int:
            total = 0
            if not p.exists():
                return 0
            for root, dirs, files in os.walk(p):
                if any(Path(root) == e or e in Path(root).parents for e in exclude):
                    dirs[:] = []
                    continue
                for f in files:
                    try:
                        total += os.lstat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
            return total

        game = size(paths.game, (paths.savegame, paths.image_backups))
        usage = shutil.disk_usage(self.settings.data_dir)
        out = {
            "game_bytes": game,
            "savegame_bytes": size(paths.savegame),
            "backups_bytes": size(paths.backups),
            "image_backups_bytes": size(paths.image_backups),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "warnings": [],
        }
        if usage.free < usage.total * 0.10:
            out["warnings"].append("Less than 10% of the disk is free")
        if game and usage.free < 2 * game:
            out["warnings"].append("Less than twice the game's size is free; game updates "
                                   "may fail")
        self._disk_cache[world.id] = (time.monotonic(), out)
        return out
