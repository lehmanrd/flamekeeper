"""Long-running operations as Jobs: persisted, observable, cancelable, serialized per world."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.logging_setup import redact
from app.models import Job
from app.services.bus import Bus

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")
FLUSH_INTERVAL_S = 1.0
MAX_LOG_CHARS = 2_000_000


class JobFailed(Exception):
    """Raise inside a job to fail it with a user-facing message (and optional hint)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "world_id": job.world_id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class JobContext:
    def __init__(self, manager: JobManager, job_id: str, world_id: str | None) -> None:
        self.manager = manager
        self.job_id = job_id
        self.world_id = world_id
        self._log_buffer: list[str] = []
        self._progress: int | None = None
        self._message = ""
        self._last_flush = 0.0
        self._pending: asyncio.TimerHandle | None = None

    def log(self, line: str) -> None:
        line = redact(line)
        self._log_buffer.append(line)
        self.manager.bus.publish(f"job:{self.job_id}", {"type": "log", "line": line})
        self._maybe_flush()

    def progress(self, pct: int | None, message: str | None = None) -> None:
        changed = pct != self._progress or (message is not None and message != self._message)
        self._progress = pct
        if message is not None:
            self._message = message
        if changed:
            self._maybe_flush(force=pct in (0, 100))

    def _maybe_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()
        elif self._pending is None:
            # Throttled: make sure the latest progress still goes out shortly, even if
            # the job then works silently for a while.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            delay = FLUSH_INTERVAL_S - (now - self._last_flush)
            self._pending = loop.call_later(delay, self.flush)

    def flush(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        self._last_flush = time.monotonic()
        lines, self._log_buffer = self._log_buffer, []
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job is None:
                return
            if lines:
                job.log = (job.log + "\n".join(lines) + "\n")[-MAX_LOG_CHARS:]
            job.progress = self._progress
            job.message = self._message
            data = job_to_dict(job)
        self.manager.publish(data)


JobFn = Callable[[JobContext], Awaitable[None]]


class JobManager:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._world_locks: dict[str, asyncio.Lock] = {}
        # Called with a job when it finishes (notifications). Must not block or raise.
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def world_lock(self, world_id: str) -> asyncio.Lock:
        return self._world_locks.setdefault(world_id, asyncio.Lock())

    def is_busy(self, world_id: str) -> bool:
        return self.world_lock(world_id).locked()

    def publish(self, job: dict[str, Any]) -> None:
        self.bus.publish(f"job:{job['id']}", {"type": "job", "job": job})
        self.bus.publish("jobs", job)

    def recover_interrupted(self) -> int:
        """Mark jobs left queued/running by a previous controller process as failed."""
        with session_scope() as s:
            jobs = s.scalars(select(Job).where(Job.status.in_(("queued", "running")))).all()
            for job in jobs:
                job.status = "failed"
                job.message = "Interrupted by controller restart"
                job.finished_at = datetime.now(UTC)
            return len(jobs)

    def submit(self, job_type: str, world_id: str | None, fn: JobFn, message: str = "") -> str:
        with session_scope() as s:
            job = Job(type=job_type, world_id=world_id, status="queued", message=message)
            s.add(job)
            s.flush()
            job_id = job.id
            data = job_to_dict(job)
        self.publish(data)
        task = asyncio.get_running_loop().create_task(self._run(job_id, world_id, fn))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))
        return job_id

    async def _run(self, job_id: str, world_id: str | None, fn: JobFn) -> None:
        ctx = JobContext(self, job_id, world_id)
        lock = self.world_lock(world_id) if world_id else None
        status, message = "failed", ""
        try:
            if lock:
                if lock.locked():
                    ctx.log("Waiting for another operation on this world to finish…")
                    ctx.flush()
                await lock.acquire()
            try:
                self._set(job_id, status="running", started_at=datetime.now(UTC))
                await fn(ctx)
                status, message = "succeeded", ctx._message or "Done"
                if ctx._progress is not None:
                    ctx._progress = 100
            finally:
                if lock:
                    lock.release()
        except asyncio.CancelledError:
            status, message = "cancelled", "Cancelled"
            ctx.log("Job cancelled.")
        except JobFailed as e:
            status, message = "failed", str(e)
            ctx.log(f"ERROR: {e}")
            if e.hint:
                ctx.log(f"HINT: {e.hint}")
        except Exception as e:
            log.exception("job %s failed", job_id)
            status, message = "failed", f"{type(e).__name__}: {e}"
            ctx.log(f"ERROR: {message}")
        finally:
            ctx._message = message
            ctx.flush()
            self._set(job_id, status=status, message=message, finished_at=datetime.now(UTC))

    def _set(self, job_id: str, **fields: Any) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            data = job_to_dict(job)
        self.publish(data)
        if fields.get("status") in TERMINAL:
            for listener in self.listeners:
                try:
                    listener(data)
                except Exception:
                    log.exception("job listener failed")

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
