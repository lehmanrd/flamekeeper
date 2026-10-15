"""World state derivation. Pure functions; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

A2S_FAILURES_UNRESPONSIVE = 3
CRASH_LOOP_RESTARTS = 3
CRASH_LOOP_WINDOW = timedelta(minutes=10)

STATES = ("stopped", "starting", "updating", "online", "unresponsive", "crashed", "missing")


@dataclass
class StateInputs:
    container_exists: bool
    container_status: str | None = None  # docker State.Status
    exit_code: int | None = None
    host_online_seen: bool = False  # 'HostOnline' seen since the last (re)start
    a2s_ok: bool = False
    a2s_failures: int = 0  # consecutive failed A2S polls
    update_running: bool = False  # update job running, or steamcmd activity in logs
    # The user asked for the world to be stopped: a non-zero exit is not a crash.
    desired_stopped: bool = False
    # Times the container (or server process) was seen starting, for crash-loop detection.
    recent_starts: tuple[datetime, ...] = ()
    now: datetime | None = None


def is_crash_loop(starts: tuple[datetime, ...], now: datetime) -> bool:
    window_start = now - CRASH_LOOP_WINDOW
    return sum(1 for t in starts if t >= window_start) > CRASH_LOOP_RESTARTS


def derive_state(i: StateInputs) -> str:
    if not i.container_exists:
        return "missing"
    status = i.container_status or "unknown"
    if status == "restarting":
        return "crashed"
    if status in ("exited", "dead"):
        crashed = (i.exit_code or 0) != 0 and not i.desired_stopped
        return "crashed" if crashed else "stopped"
    if status == "created":
        return "stopped"
    if status not in ("running", "paused"):
        return "stopped"
    if i.now and is_crash_loop(i.recent_starts, i.now):
        return "crashed"
    if i.update_running:
        return "updating"
    if not i.host_online_seen:
        return "starting"
    if i.a2s_failures >= A2S_FAILURES_UNRESPONSIVE:
        return "unresponsive"
    if i.a2s_ok:
        return "online"
    # HostOnline seen but A2S not answered yet: still coming up.
    return "starting"
