"""Server-Sent Event streams: world status, live logs, job progress."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_services
from app.db import session_scope
from app.models import Job
from app.services.container import Services
from app.services.jobs import TERMINAL, job_to_dict
from app.services.log_lines import is_noise, parse_line
from app.services.world_paths import container_name

router = APIRouter(prefix="/stream")

PING_S = 15


def _event(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, default=str)}


@router.get("/worlds")
async def stream_worlds(request: Request, services: Services = Depends(get_services)):
    async def gen() -> AsyncIterator[dict[str, str]]:
        async with services.bus.subscribe("worlds") as q:
            snapshot = [services.monitor.summary(w) for w in services.worlds.list()]
            yield _event("snapshot", snapshot)
            async with services.bus.subscribe("jobs") as jq:
                while not await request.is_disconnected():
                    get_w = asyncio.ensure_future(q.get())
                    get_j = asyncio.ensure_future(jq.get())
                    done, pending = await asyncio.wait({get_w, get_j}, timeout=PING_S,
                                                       return_when=asyncio.FIRST_COMPLETED)
                    for p in pending:
                        p.cancel()
                    if get_w in done:
                        yield _event("world", get_w.result())
                    if get_j in done:
                        yield _event("job", get_j.result())

    return EventSourceResponse(gen(), ping=PING_S)


@router.get("/worlds/{world_id}/logs")
async def stream_logs(
    world_id: str,
    request: Request,
    tail: int = Query(500, ge=0, le=5000),
    services: Services = Depends(get_services),
):
    world = services.worlds.get(world_id)
    name = container_name(world.slug)
    if await services.docker.get_container(name) is None:
        raise HTTPException(404, "World has no container")

    async def gen() -> AsyncIterator[dict[str, str]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for raw in services.docker.logs_follow(name, tail=tail):
                    parsed = parse_line(raw)
                    if not is_noise(parsed):
                        queue.put_nowait(parsed)
            finally:
                queue.put_nowait(None)

        producer = asyncio.create_task(produce())
        try:
            while not await request.is_disconnected():
                try:
                    first = await asyncio.wait_for(queue.get(), PING_S)
                except TimeoutError:
                    continue
                # Batch bursts (the initial tail, steamcmd floods) into one message.
                batch, ended = [], first is None
                if first is not None:
                    batch.append(first)
                while not ended and not queue.empty() and len(batch) < 500:
                    item = queue.get_nowait()
                    if item is None:
                        ended = True
                    else:
                        batch.append(item)
                if batch:
                    yield _event("lines", batch)
                if ended:
                    yield _event("end", {"reason": "container stopped"})
                    return
        finally:
            producer.cancel()

    return EventSourceResponse(gen(), ping=PING_S)


@router.get("/jobs/{job_id}")
async def stream_job(job_id: str, request: Request, services: Services = Depends(get_services)):
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        initial = {**job_to_dict(job), "log": job.log}

    async def gen() -> AsyncIterator[dict[str, str]]:
        async with services.bus.subscribe(f"job:{job_id}") as q:
            yield _event("snapshot", initial)
            if initial["status"] in TERMINAL:
                return
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), PING_S)
                except TimeoutError:
                    continue
                if msg["type"] == "log":
                    yield _event("log", msg["line"])
                else:
                    yield _event("job", msg["job"])
                    if msg["job"]["status"] in TERMINAL:
                        return

    return EventSourceResponse(gen(), ping=PING_S)
