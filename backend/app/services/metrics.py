"""Resource metrics: one sample per minute per running world, kept 7 days.

CPU, memory and network come from `docker stats`; the player count from the monitor's
A2S polls. Host CPU and memory come from /proc, which a container shares with the host.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.db import session_scope
from app.models import MetricSample, World
from app.services.docker_adapter import DockerAdapter
from app.services.world_paths import container_name

log = logging.getLogger(__name__)

SAMPLE_EVERY_S = 60.0
RANGES = {"1h": (timedelta(hours=1), 60), "24h": (timedelta(hours=24), 300),
          "7d": (timedelta(days=7), 1800)}


@dataclass
class _Last:
    mono: float
    rx: int
    tx: int


class MetricsService:
    def __init__(self, docker: DockerAdapter, sample_every: float = SAMPLE_EVERY_S,
                 retention_days: Callable[[], int] = lambda: 7) -> None:
        self.docker = docker
        self.retention_days = retention_days
        self.sample_every = sample_every
        self._last: dict[str, _Last] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._last_prune = 0.0
        self._cpu_prev: tuple[int, int] | None = None

    def due(self, world_id: str) -> bool:
        last = self._last.get(world_id)
        return last is None or time.monotonic() - last.mono >= self.sample_every

    async def sample(self, world: World, players: int | None) -> dict[str, Any] | None:
        stats = await self.docker.stats(container_name(world.slug))
        now = time.monotonic()
        if stats is None:
            self._last.pop(world.id, None)
            self._latest.pop(world.id, None)
            return None
        prev = self._last.get(world.id)
        rx_bps = tx_bps = None
        if prev and now > prev.mono and stats.net_rx >= prev.rx and stats.net_tx >= prev.tx:
            rx_bps = (stats.net_rx - prev.rx) / (now - prev.mono)
            tx_bps = (stats.net_tx - prev.tx) / (now - prev.mono)
        self._last[world.id] = _Last(now, stats.net_rx, stats.net_tx)
        row = {
            "ts": datetime.now(UTC).replace(microsecond=0),
            "cpu_pct": stats.cpu_pct,
            "mem_mb": round(stats.mem_bytes / 1024**2, 1) if stats.mem_bytes is not None else None,
            "net_rx_bps": round(rx_bps) if rx_bps is not None else None,
            "net_tx_bps": round(tx_bps) if tx_bps is not None else None,
            "players": players,
        }
        with session_scope() as s:
            s.merge(MetricSample(world_id=world.id, **row))
        self._latest[world.id] = {**row, "ts": row["ts"].isoformat(),
                                  "mem_limit_mb": round(stats.mem_limit / 1024**2)
                                  if stats.mem_limit else None}
        if time.monotonic() - self._last_prune > 3600:
            self.prune()
        return row

    def latest(self, world_id: str) -> dict[str, Any] | None:
        return self._latest.get(world_id)

    def forget(self, world_id: str) -> None:
        self._last.pop(world_id, None)
        self._latest.pop(world_id, None)

    def prune(self) -> int:
        self._last_prune = time.monotonic()
        with session_scope() as s:
            keep = timedelta(days=self.retention_days())
            res = s.execute(delete(MetricSample).where(MetricSample.ts < datetime.now(UTC) - keep))
            return res.rowcount or 0

    def query(self, world_id: str, range_: str) -> dict[str, Any]:
        window, step = RANGES[range_]
        since = datetime.now(UTC) - window
        with session_scope() as s:
            rows = s.scalars(select(MetricSample).where(MetricSample.world_id == world_id,
                                                        MetricSample.ts >= since)
                             .order_by(MetricSample.ts)).all()
            samples = [(_aware(r.ts), r.cpu_pct, r.mem_mb, r.net_rx_bps, r.net_tx_bps,
                        r.players) for r in rows]
        buckets: dict[int, list[tuple]] = {}
        for sample in samples:
            buckets.setdefault(int(sample[0].timestamp()) // step * step, []).append(sample)
        points = []
        for start, group in sorted(buckets.items()):
            def avg(i: int, group=group) -> float | None:
                vals = [g[i] for g in group if g[i] is not None]
                return round(sum(vals) / len(vals), 2) if vals else None

            players = [g[5] for g in group if g[5] is not None]
            points.append({"ts": datetime.fromtimestamp(start, UTC).isoformat(),
                           "cpu_pct": avg(1), "mem_mb": avg(2), "net_rx_bps": avg(3),
                           "net_tx_bps": avg(4), "players": max(players) if players else None})
        return {"range": range_, "step_s": step, "points": points}

    def host(self) -> dict[str, Any]:
        """Host CPU % since the previous call, and memory, from /proc."""
        cpu_pct = None
        try:
            with open("/proc/stat") as fh:
                parts = [int(x) for x in fh.readline().split()[1:]]
            idle, total = parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)
            if self._cpu_prev and total > self._cpu_prev[1]:
                d_total = total - self._cpu_prev[1]
                cpu_pct = round(100 * (1 - (idle - self._cpu_prev[0]) / d_total), 1)
            self._cpu_prev = (idle, total)
        except (OSError, ValueError, IndexError):
            pass
        mem: dict[str, int] = {}
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    if key in ("MemTotal", "MemAvailable"):
                        mem[key] = int(rest.split()[0]) * 1024
        except (OSError, ValueError):
            pass
        total = mem.get("MemTotal")
        avail = mem.get("MemAvailable")
        return {"cpu_pct": cpu_pct, "cpus": os.cpu_count(), "mem_total": total,
                "mem_used": total - avail if total and avail is not None else None}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
