"""Runs scheduled work: backups now, update checks and restarts later.

The controller owns schedules, so the UI can show history, progress and the next run.
Each tick checks every world's schedules against their cron expressions.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.services.backup_service import BackupService
from app.services.update_service import UpdateService
from app.services.world_service import WorldService

log = logging.getLogger(__name__)

TICK_S = 30.0
PRUNE_EVERY_S = 3600.0


class Scheduler:
    def __init__(self, worlds: WorldService, backups: BackupService,
                 updates: UpdateService | None = None, tick: float = TICK_S) -> None:
        self.worlds = worlds
        self.backups = backups
        self.updates = updates
        self.tick = tick
        self._task: asyncio.Task[None] | None = None
        self._last_prune = 0.0

    def start(self) -> None:
        for world in self.worlds.list():
            self.backups.ensure_default_schedule(world)
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                self.run_once()
                if self.updates:
                    await self.updates.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick)

    def run_once(self) -> list[str]:
        """One pass over all worlds; returns the jobs it started."""
        started = []
        prune = time.monotonic() - self._last_prune >= PRUNE_EVERY_S
        for world in self.worlds.list():
            try:
                job = self.backups.run_due(world)
                if job:
                    started.append(job)
                if prune:
                    self.backups.prune(world)
            except Exception:
                log.exception("scheduled backup failed to start for %s", world.slug)
        if prune:
            self._last_prune = time.monotonic()
        return started
