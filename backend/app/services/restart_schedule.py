"""Scheduled restarts: restart a long-running server on a cron schedule.

Long uptimes make the Enshrouded server less stable, so an admin can have each world
restarted at a quiet time. The schedule lives in the `schedules` table (kind "restart").

Waiting for players to leave happens here, between scheduler passes, not inside the
restart job: jobs on a world run one at a time, and a job that waited for hours would
hold up that world's backups and updates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.db import session_scope
from app.models import Schedule, World
from app.services.docker_adapter import DockerAdapter
from app.services.events import EventLog
from app.services.jobs import JobManager
from app.services.schedules import aware, next_run, next_runs, now, timezone_name, validate_cron
from app.services.world_paths import container_name
from app.services.world_service import WorldError, WorldService

log = logging.getLogger(__name__)

KIND = "restart"
DEFAULTS: dict[str, Any] = {"cron": "0 5 * * *", "enabled": False, "only_when_empty": True,
                            "max_wait_min": 120, "if_still_busy": "skip", "min_uptime_h": 2}
FIELDS = tuple(DEFAULTS)
IF_STILL_BUSY = ("skip", "restart")
MIN_INTERVAL = timedelta(hours=1)
MAX_WAIT_MIN = 24 * 60
MAX_UPTIME_H = 7 * 24
# States in which the server is (re)starting or updating on its own: not restarted.
BUSY_STATES = ("starting", "updating")


class RestartScheduler:
    def __init__(self, worlds: WorldService, docker: DockerAdapter, jobs: JobManager,
                 events: EventLog) -> None:
        self.worlds = worlds
        self.docker = docker
        self.jobs = jobs
        self.events = events
        # world id -> when its due restart started waiting (players online, or busy).
        self._pending: dict[str, datetime] = {}

    # -- settings -------------------------------------------------------------------

    def ensure_default(self, world: World) -> None:
        with session_scope() as s:
            if s.get(Schedule, (world.id, KIND)) is None:
                s.add(Schedule(world_id=world.id, kind=KIND, retention_count=0,
                               skip_if_idle=False, max_age_days=0, last_run_at=now(),
                               **DEFAULTS))

    def row(self, world: World) -> Schedule:
        self.ensure_default(world)
        with session_scope() as s:
            row = s.get(Schedule, (world.id, KIND))
            s.expunge(row)
            return row

    def schedule(self, world: World) -> dict[str, Any]:
        row = self.row(world)
        nxt = next_run(row.cron, row.last_run_at) if row.enabled else None
        waiting = self._pending.get(world.id) if row.enabled else None
        return {
            "kind": KIND,
            **{k: getattr(row, k) for k in FIELDS},
            "last_run_at": aware(row.last_run_at).isoformat() if row.last_run_at else None,
            "next_run_at": nxt.isoformat() if nxt else None,
            "waiting_since": waiting.isoformat() if waiting else None,
            "upcoming": [d.isoformat() for d in next_runs(row.cron, now(), 3)],
            "timezone": timezone_name(),
        }

    def update_schedule(self, world: World, changes: dict[str, Any]) -> dict[str, Any]:
        if "cron" in changes:
            validate_cron(changes["cron"], MIN_INTERVAL,
                          "Restarts can run at most once an hour")
        if not 0 <= changes.get("max_wait_min", 0) <= MAX_WAIT_MIN:
            raise WorldError(f"Wait must be between 0 and {MAX_WAIT_MIN} minutes", 422)
        if not 0 <= changes.get("min_uptime_h", 0) <= MAX_UPTIME_H:
            raise WorldError(f"Minimum uptime must be between 0 and {MAX_UPTIME_H} hours", 422)
        if changes.get("if_still_busy", "skip") not in IF_STILL_BUSY:
            raise WorldError("If still busy must be 'skip' or 'restart'", 422)
        self.ensure_default(world)
        with session_scope() as s:
            row = s.get(Schedule, (world.id, KIND))
            for k in FIELDS:
                if k in changes:
                    setattr(row, k, changes[k].strip() if k == "cron" else changes[k])
            if changes.get("enabled") or "cron" in changes:
                # Count from now: a schedule just turned on doesn't fire for missed slots.
                row.last_run_at = now()
                self._pending.pop(world.id, None)
            if changes.get("enabled") is False:
                self._pending.pop(world.id, None)
        return self.schedule(world)

    # -- running --------------------------------------------------------------------

    async def run_due(self, world: World, now_: datetime | None = None) -> str | None:
        """Start the scheduled restart if it's due and allowed. Returns the job id."""
        current = now_ or now()
        row = self.row(world)
        if not row.enabled:
            self._pending.pop(world.id, None)
            return None
        since = self._pending.get(world.id)
        if since is None:
            nxt = next_run(row.cron, row.last_run_at)
            if nxt is None or nxt > current:
                return None
            # This slot is handled now, whatever happens next: no backlog after downtime.
            with session_scope() as s:
                s.get(Schedule, (world.id, KIND)).last_run_at = current
            if not await self._running(world):
                log.info("scheduled restart skipped: the world is not running",
                         extra={"data": {"world": world.slug}})
                return None
            state = self._state(world)
            last_start = self._last_start(world)
            if state in BUSY_STATES or (
                    last_start and current - last_start < timedelta(hours=row.min_uptime_h)):
                self._skipped(world, "recent_start")
                return None
            since = self._pending[world.id] = current
        else:
            if not await self._running(world):
                self._pending.pop(world.id, None)
                return None
            last_start = self._last_start(world)
            if last_start and last_start > since:
                self._pending.pop(world.id, None)
                self._skipped(world, "restarted_meanwhile")
                return None

        if self.jobs.is_busy(world.id) or self._state(world) in BUSY_STATES:
            return None  # try again on the next pass
        players = self._players(world)
        if players and row.only_when_empty:
            if current - since < timedelta(minutes=row.max_wait_min):
                return None
            if row.if_still_busy != "restart":
                self._pending.pop(world.id, None)
                self._skipped(world, "players_online", players=players)
                return None
        self._pending.pop(world.id, None)
        self.events.record(world.id, "scheduled_restart", outcome="restarting",
                           players=players)
        return self.worlds.restart(world, reason="scheduled")

    async def _running(self, world: World) -> bool:
        current = self.worlds.get(world.id)
        if current.desired_state != "running":
            return False
        info = await self.docker.get_container(container_name(current.slug))
        return bool(info and info.running)

    def _state(self, world: World) -> str | None:
        monitor = self.worlds.monitor
        return monitor.runtime(world.id).state if monitor else None

    def _players(self, world: World) -> int:
        monitor = self.worlds.monitor
        return (monitor.player_count(world.id) if monitor else None) or 0

    def _last_start(self, world: World) -> datetime | None:
        """When the server was last started or came online after an update or crash.

        "started" covers a world's first boot, whose "online" the monitor doesn't record.
        """
        for e in self.events.recent(world.id, 10, kinds=["online", "started"]):
            if not e["data"].get("recovered"):
                return aware(datetime.fromisoformat(e["ts"]))
        return None

    def _skipped(self, world: World, reason: str, **data: Any) -> None:
        log.info("scheduled restart skipped",
                 extra={"data": {"world": world.slug, "reason": reason, **data}})
        self.events.record(world.id, "scheduled_restart", outcome="skipped", reason=reason,
                           **data)
