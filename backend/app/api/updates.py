"""Updates, metrics and player history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import get_services, require_ready
from app.db import session_scope
from app.models import PlayerSession
from app.schemas import JobRef, UpdatePolicy, UpdateRequest
from app.services.container import Services
from app.services.metrics import RANGES

router = APIRouter()


@router.get("/worlds/{world_id}/update")
async def update_status(world_id: str, services: Services = Depends(get_services)) -> dict:
    return services.updates.status(services.worlds.get(world_id))


@router.post("/worlds/{world_id}/update/check")
async def check_now(world_id: str, services: Services = Depends(get_services)) -> dict:
    world = services.worlds.get(world_id)
    status = await services.updates.check(world, force=True)
    services.monitor.notify(world.id)
    return status


@router.post("/worlds/{world_id}/update", status_code=202)
async def start_update(
    world_id: str, body: UpdateRequest | None = None, services: Services = Depends(require_ready)
) -> JobRef:
    body = body or UpdateRequest()
    world = services.worlds.get(world_id)
    return JobRef(job_id=services.updates.update(world, target=body.target, when=body.when,
                                                 force=body.force))


@router.put("/worlds/{world_id}/update-policy")
async def set_policy(
    world_id: str, body: UpdatePolicy, services: Services = Depends(require_ready)
) -> dict:
    world = services.worlds.get(world_id)
    return services.updates.set_policy(world, body.model_dump(exclude_none=True))


@router.get("/worlds/{world_id}/update/history")
async def update_history(world_id: str, services: Services = Depends(get_services)) -> list:
    return services.updates.history(services.worlds.get(world_id))


@router.get("/worlds/{world_id}/metrics")
async def metrics(
    world_id: str,
    range: str = Query("1h", pattern="^(1h|24h|7d)$"),  # noqa: A002 - API name
    services: Services = Depends(get_services),
) -> dict:
    world = services.worlds.get(world_id)
    return services.metrics.query(world.id, range)


@router.get("/worlds/{world_id}/players/history")
async def player_history(
    world_id: str,
    limit: int = Query(100, ge=1, le=1000),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    """Recent sessions and per-player totals."""
    world = services.worlds.get(world_id)
    now = datetime.now(UTC)
    with session_scope() as s:
        rows = s.scalars(select(PlayerSession).where(PlayerSession.world_id == world.id)
                         .order_by(PlayerSession.joined_at.desc())).all()
        sessions = [(r.player_name, _aware(r.joined_at), _aware(r.left_at) if r.left_at
                     else None) for r in rows]
    totals: dict[str, dict[str, Any]] = {}
    for name, joined, left in sessions:
        t = totals.setdefault(name, {"player": name, "sessions": 0, "playtime_s": 0,
                                     "last_seen": None, "online": False})
        t["sessions"] += 1
        t["playtime_s"] += int(((left or now) - joined).total_seconds())
        seen = left or now
        if t["last_seen"] is None or seen > t["last_seen"]:
            t["last_seen"] = seen
        t["online"] = t["online"] or left is None
    players = sorted(totals.values(), key=lambda t: t["last_seen"], reverse=True)
    for t in players:
        t["last_seen"] = t["last_seen"].isoformat()
    return {
        "players": players,
        "sessions": [{"player": n, "joined_at": j.isoformat(),
                      "left_at": lf.isoformat() if lf else None,
                      "duration_s": int(((lf or now) - j).total_seconds())}
                     for n, j, lf in sessions[:limit]],
    }


@router.get("/system/host")
async def host(services: Services = Depends(get_services)) -> dict[str, Any]:
    """Host CPU/RAM/disk plus totals across worlds, for the overview strip."""
    import shutil

    data_dir = services.settings.data_dir
    usage = shutil.disk_usage(data_dir) if data_dir.exists() else None
    worlds = services.worlds.list()
    players = 0
    slots = 0
    for w in worlds:
        rt = services.monitor.runtime(w.id)
        players += rt.players or 0
        slots += rt.max_players or 0
    return {
        **services.metrics.host(),
        "disk_total": usage.total if usage else None,
        "disk_free": usage.free if usage else None,
        "data_dir": str(data_dir),
        "worlds": len(worlds),
        "players": players,
        "slots": slots,
        "ranges": list(RANGES),
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
