"""Cron helpers shared by the world schedules (backups, restarts) and update windows.

Expressions are 5-field crontab lines read in the controller's time zone (TZ).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone

from app.services.world_service import WorldError

MIN_INTERVAL = timedelta(minutes=10)


def now() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
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
    current = after
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, current)
        if nxt is None:
            break
        out.append(nxt)
        prev, current = nxt, nxt + timedelta(seconds=1)
    return out


def next_run(expr: str, last_run_at: datetime | None) -> datetime | None:
    """The first slot after the last run (or after now, for a schedule that never ran)."""
    after = aware(last_run_at) if last_run_at else now()
    runs = next_runs(expr, after + timedelta(seconds=1), 1)
    return runs[0] if runs else None


def validate_cron(expr: str, min_interval: timedelta = MIN_INTERVAL,
                  too_often: str = "Backups can run at most every 10 minutes") -> None:
    runs = next_runs(expr, now(), 4)
    if len(runs) < 2:
        raise WorldError("The schedule never runs (or only once)", 422)
    gaps = [b - a for a, b in zip(runs, runs[1:], strict=False)]
    if min(gaps) < min_interval:
        raise WorldError(too_often, 422)


def timezone_name() -> str:
    return str(get_localzone())
