"""Live world monitoring: Docker state, log watching, A2S polling.

Covers state derivation, HostOnline/steamcmd/player log parsing, A2S player counts and
state-change events; resource metrics are sampled from here too.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import a2s
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.models import PlayerSession, World
from app.services.bus import Bus
from app.services.docker_adapter import DockerAdapter, split_timestamp
from app.services.events import EventLog
from app.services.jobs import JobManager
from app.services.log_patterns import load_patterns
from app.services.metrics import MetricsService
from app.services.state import StateInputs, derive_state
from app.services.world_paths import WorldPaths, container_name, installed_build

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0
# A player who just logged in may not be counted by A2S yet.
SESSION_GRACE = timedelta(seconds=60)
# A requested start whose start line hasn't appeared by then (first boots download the
# game first) no longer excuses the next start from crash-loop detection.
EXPECTED_START_TTL = timedelta(minutes=45)
A2S_TIMEOUT_S = 3.0


@dataclass
class Runtime:
    world_id: str
    state: str = "unknown"
    container_status: str | None = None
    container_labels: dict[str, str] = field(default_factory=dict)
    started_at: datetime | None = None
    host_online_seen: bool = False
    online_since: datetime | None = None
    a2s_ok: bool = False
    a2s_failures: int = 0
    players: int | None = None
    max_players: int | None = None
    player_names: list[str] = field(default_factory=list)
    server_name: str | None = None
    server_version: str | None = None
    update_running: bool = False
    update_progress: float | None = None
    recent_starts: deque[datetime] = field(default_factory=lambda: deque(maxlen=20))
    # Starts requested by the user; these don't count towards crash-loop detection.
    # expected_since: when the oldest of them was requested. A container start's log is
    # read from the container's start, so its start line usually arrives as a replay.
    expected_starts: int = 0
    expected_since: datetime | None = None
    last_failure: dict[str, Any] | None = None
    # Consecutive A2S polls that reported 0 players (closes sessions the log missed).
    zero_polls: int = 0
    # False until the first steady state after controller start, so a restart of the
    # controller doesn't log events for states the world was already in.
    settled: bool = False
    watcher: asyncio.Task[None] | None = None
    watched_key: tuple[str, datetime | None] | None = None


class Monitor:
    def __init__(
        self,
        settings: Settings,
        docker: DockerAdapter,
        bus: Bus,
        events: EventLog,
        jobs: JobManager,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self.settings = settings
        self.docker = docker
        self.bus = bus
        self.events = events
        self.jobs = jobs
        self.poll_interval = poll_interval
        self.runtimes: dict[str, Runtime] = {}
        # Set by app wiring: does this container need a recreate for its infra settings?
        self.recreate_check: Callable[[World, dict[str, str]], bool] | None = None
        # Set by app wiring: resource metrics and update info for summaries.
        self.metrics: MetricsService | None = None
        self.update_summary: Callable[[World], dict[str, Any]] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._patterns = load_patterns()
        self._last_summary: dict[str, dict[str, Any]] = {}

    # -- control ------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="monitor")

    async def stop(self) -> None:
        tasks = [rt.watcher for rt in self.runtimes.values() if rt.watcher]
        if self._task:
            tasks.append(self._task)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def notify(self, world_id: str | None = None) -> None:
        """Ask for an immediate poll (after a lifecycle action)."""
        self._wake.set()

    def forget(self, world_id: str) -> None:
        rt = self.runtimes.pop(world_id, None)
        if rt and rt.watcher:
            rt.watcher.cancel()
        self._last_summary.pop(world_id, None)
        self.bus.publish("worlds", {"id": world_id, "deleted": True})

    def expect_restart(self, world_id: str) -> None:
        rt = self.runtime(world_id)
        rt.host_online_seen = False
        rt.a2s_ok = False
        if rt.expected_starts == 0:
            rt.expected_since = datetime.now(UTC)
        rt.expected_starts += 1

    def runtime(self, world_id: str) -> Runtime:
        return self.runtimes.setdefault(world_id, Runtime(world_id))

    def player_count(self, world_id: str) -> int | None:
        rt = self.runtimes.get(world_id)
        return rt.players if rt else None

    # -- loop ---------------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except TimeoutError:
                pass

    async def tick(self) -> None:
        with session_scope() as s:
            worlds = s.scalars(select(World)).all()
            for w in worlds:
                s.expunge(w)
        await asyncio.gather(*(self._poll_world(w) for w in worlds))

    async def _poll_world(self, world: World) -> None:
        rt = self.runtime(world.id)
        name = container_name(world.slug)
        info = await self.docker.get_container(name)
        if info is None:
            rt.container_status = None
            rt.container_labels = {}
            self._stop_watcher(rt)
        else:
            rt.container_status = info.status
            rt.container_labels = info.labels
            if info.running:
                key = (info.id, info.started_at)
                if rt.watched_key != key or rt.watcher is None or rt.watcher.done():
                    self._start_watcher(world, rt, key)
            else:
                self._stop_watcher(rt)
                rt.host_online_seen = False
                rt.a2s_ok = False
                rt.players = None
                rt.update_running = False

        if info and info.running and rt.host_online_seen:
            await self._poll_a2s(world, rt)
        if info and info.running and self.metrics and self.metrics.due(world.id):
            try:
                await self.metrics.sample(world, rt.players)
            except Exception:
                log.exception("metrics sample failed for %s", world.slug)

        state = derive_state(
            StateInputs(
                container_exists=info is not None,
                container_status=info.status if info else None,
                exit_code=info.exit_code if info else None,
                host_online_seen=rt.host_online_seen,
                a2s_ok=rt.a2s_ok,
                a2s_failures=rt.a2s_failures,
                update_running=rt.update_running,
                desired_stopped=world.desired_state == "stopped",
                recent_starts=tuple(rt.recent_starts),
                now=datetime.now(UTC),
            )
        )
        self._transition(world, rt, state)
        self._publish_summary(world, rt)

    def _transition(self, world: World, rt: Runtime, state: str) -> None:
        prev = rt.state
        rt.state = state
        if prev == state:
            return
        if state == "online":
            rt.online_since = datetime.now(UTC)
        elif state not in ("unresponsive",):
            rt.online_since = None
        if not rt.settled:
            # Until the log replay after a controller start settles, "online" or
            # "stopped" only re-discovers the existing state; a crash is still news.
            rt.settled = state != "starting"
            if prev == "unknown" or state in ("online", "stopped"):
                return
        if state == "online":
            self.events.record(world.id, "online")
        elif state == "crashed":
            self.events.record(world.id, "crashed", previous=prev)
        elif state == "unresponsive":
            self.events.record(world.id, "warning", message="Server stopped answering queries")

    # -- A2S ----------------------------------------------------------------------------

    def a2s_address(self, world: World) -> tuple[str, int]:
        if self.settings.resolved_a2s_mode == "network":
            return container_name(world.slug), world.query_port
        host = world.bind_ip if world.bind_ip not in ("", "0.0.0.0") else self.settings.a2s_host
        return host, world.query_port

    async def _poll_a2s(self, world: World, rt: Runtime) -> None:
        if self.settings.fake_docker:
            rt.a2s_ok, rt.a2s_failures = True, 0
            rt.players = rt.players or 0
            return
        addr = self.a2s_address(world)
        try:
            info = await a2s.ainfo(addr, timeout=A2S_TIMEOUT_S)
            rt.a2s_ok, rt.a2s_failures = True, 0
            rt.players = info.player_count
            self._reconcile_sessions(world, rt)
            rt.max_players = info.max_players
            rt.server_name = info.server_name
            rt.server_version = info.version
            try:
                players = await a2s.aplayers(addr, timeout=A2S_TIMEOUT_S)
                rt.player_names = [p.name for p in players if p.name]
            except Exception:
                rt.player_names = []
        except Exception as e:
            rt.a2s_failures += 1
            if rt.a2s_failures >= 3:
                rt.a2s_ok = False
            log.debug("A2S %s failed (%d): %s", addr, rt.a2s_failures, e)

    # -- log watcher ----------------------------------------------------------------------

    def _stop_watcher(self, rt: Runtime) -> None:
        if rt.watcher:
            rt.watcher.cancel()
        rt.watcher = None
        rt.watched_key = None

    def _start_watcher(self, world: World, rt: Runtime, key: tuple[str, datetime | None]) -> None:
        self._stop_watcher(rt)
        rt.watched_key = key
        rt.host_online_seen = False
        rt.a2s_ok = False
        rt.a2s_failures = 0
        rt.update_running = False
        rt.last_failure = None
        self._close_open_sessions(world.id)
        rt.watcher = asyncio.get_running_loop().create_task(
            self._watch(world, rt, key[1]), name=f"watch-{world.slug}"
        )

    async def _watch(self, world: World, rt: Runtime, since: datetime | None) -> None:
        live_from = datetime.now(UTC)
        name = container_name(world.slug)
        try:
            async for raw in self.docker.logs_follow(name, since=since, tail=None,
                                                     timestamps=True):
                ts, line = split_timestamp(raw)
                live = ts is None or ts >= live_from
                self.handle_line(world, rt, line, live=live, ts=ts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("log watcher for %s stopped: %s", world.slug, e)
        finally:
            self._wake.set()

    def handle_line(
        self, world: World, rt: Runtime, line: str, *, live: bool, ts: datetime | None = None
    ) -> None:
        ev = self._patterns.classify(line)
        if ev is None:
            return
        now = ts or datetime.now(UTC)
        changed = True
        if ev.kind == "host_online":
            rt.host_online_seen = True
            rt.update_running = False
            if rt.last_failure and not rt.last_failure.get("sticky"):
                rt.last_failure = None  # whatever went wrong before, the server is up now
        elif ev.kind == "server_process_start":
            rt.host_online_seen = False
            rt.a2s_ok = False
            rt.update_running = False
            if rt.expected_since and now - rt.expected_since > EXPECTED_START_TTL:
                rt.expected_starts, rt.expected_since = 0, None  # that start never came
            if rt.expected_starts > 0 and (live or (rt.expected_since is not None
                                                    and now >= rt.expected_since)):
                rt.expected_starts -= 1
                if rt.expected_starts == 0:
                    rt.expected_since = None
            else:
                rt.recent_starts.append(now)
            if live:
                self._close_open_sessions(world.id)
        elif ev.kind == "server_shutdown":
            rt.host_online_seen = False
            rt.a2s_ok = False
        elif ev.kind == "steam_progress":
            rt.update_running = True
            rt.update_progress = ev.data["pct"]
        elif ev.kind == "steam_success":
            rt.update_progress = 100.0
            if rt.last_failure and (rt.last_failure.get("retryable")
                                    or rt.last_failure.get("sticky")):
                rt.last_failure = None  # the updater's retry (or a later update) worked
        elif ev.kind == "steam_aborted":
            # The image starts the old version anyway, so HostOnline doesn't clear this.
            cause = rt.last_failure or {}
            rt.last_failure = {
                "title": "Game update failed; the server runs the old version",
                "hint": cause.get("hint") or "Run a forced update from the Updates page.",
                "line": line, "ts": now.isoformat(), "sticky": True}
            rt.update_running = False
            if live:
                self.events.record(world.id, "warning", message=rt.last_failure["title"],
                                   hint=rt.last_failure["hint"])
        elif ev.kind == "failure":
            rt.last_failure = {**ev.data, "line": line, "ts": now.isoformat()}
            if live and not ev.data.get("retryable"):
                self.events.record(world.id, "warning", message=ev.data["title"],
                                   hint=ev.data["hint"])
        elif ev.kind == "player_joined" and live:
            self._player_joined(world.id, ev.data["name"], now)
        elif ev.kind == "player_left" and live:
            self._player_left(world.id, ev.data["name"], now)
        else:
            changed = False
        if changed and live:
            self._wake.set()

    # -- player sessions ------------------------------------------------------------------

    def _player_joined(self, world_id: str, name: str, ts: datetime) -> None:
        with session_scope() as s:
            s.add(PlayerSession(world_id=world_id, player_name=name, joined_at=ts))
        self.events.record(world_id, "player_joined", player=name)

    def _player_left(self, world_id: str, name: str, ts: datetime) -> None:
        with session_scope() as s:
            session = s.scalar(
                select(PlayerSession)
                .where(PlayerSession.world_id == world_id, PlayerSession.player_name == name,
                       PlayerSession.left_at.is_(None))
                .order_by(PlayerSession.joined_at.desc())
            )
            if session:
                session.left_at = ts
        self.events.record(world_id, "player_left", player=name)

    def _reconcile_sessions(self, world: World, rt: Runtime) -> None:
        """Close sessions the log never ended. A kicked or banned player gets no
        "Remove Player" line, only "Disconnecting peer" without a name (ban
        test), so when the server reports 0 players twice in a row, everyone left."""
        rt.zero_polls = rt.zero_polls + 1 if rt.players == 0 else 0
        if rt.zero_polls < 2:
            return
        cutoff = datetime.now(UTC) - SESSION_GRACE
        with session_scope() as s:
            stale = s.scalars(
                select(PlayerSession).where(PlayerSession.world_id == world.id,
                                            PlayerSession.left_at.is_(None))
            ).all()
            names = []
            for session in stale:
                if _aware(session.joined_at) <= cutoff:
                    session.left_at = datetime.now(UTC)
                    names.append(session.player_name)
        for name in names:
            self.events.record(world.id, "player_left", player=name, reason="disconnected")

    def _close_open_sessions(self, world_id: str) -> None:
        with session_scope() as s:
            for session in s.scalars(
                select(PlayerSession).where(PlayerSession.world_id == world_id,
                                            PlayerSession.left_at.is_(None))
            ):
                session.left_at = datetime.now(UTC)

    def online_players(self, world_id: str) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.scalars(
                select(PlayerSession)
                .where(PlayerSession.world_id == world_id, PlayerSession.left_at.is_(None))
                .order_by(PlayerSession.joined_at)
            ).all()
            return [{"name": r.player_name, "joined_at": r.joined_at.isoformat()} for r in rows]

    # -- summaries ----------------------------------------------------------------------

    def summary(self, world: World) -> dict[str, Any]:
        rt = self.runtime(world.id)
        paths = WorldPaths.for_slug(self.settings.worlds_dir, world.slug)
        return {
            "id": world.id,
            "slug": world.slug,
            "display_name": world.display_name,
            "state": rt.state if rt.state != "unknown" else "starting",
            "desired_state": world.desired_state,
            "container_name": container_name(world.slug),
            "container_status": rt.container_status,
            "query_port": world.query_port,
            "bind_ip": world.bind_ip,
            "image": f"{world.image_repo}:{world.image_tag}",
            "players": rt.players,
            "max_players": rt.max_players,
            "player_names": rt.player_names,
            "server_version": rt.server_version,
            "build_id": installed_build(paths),
            "online_since": rt.online_since.isoformat() if rt.online_since else None,
            "update_progress": rt.update_progress if rt.update_running else None,
            "last_failure": rt.last_failure,
            "busy": self.jobs.is_busy(world.id),
            "pending_restart": world.config_revision != world.applied_revision,
            "metrics": self.metrics.latest(world.id) if self.metrics else None,
            **(self.update_summary(world) if self.update_summary else {}),
            "recreate_required": bool(self.recreate_check and rt.container_status
                                      and self.recreate_check(world, rt.container_labels)),
            "config_revision": world.config_revision,
            "applied_revision": world.applied_revision,
        }

    def _publish_summary(self, world: World, rt: Runtime) -> None:
        summary = self.summary(world)
        if self._last_summary.get(world.id) != summary:
            self._last_summary[world.id] = summary
            self.bus.publish("worlds", summary)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
