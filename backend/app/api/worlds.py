"""World CRUD, lifecycle actions, logs, events, players."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.api.deps import get_services, require_ready
from app.logging_setup import redact
from app.models import World
from app.schemas import InfraPatch, JobRef, RestartRequest, StopRequest, WorldCreate
from app.services.backup_service import validate_cron
from app.services.capacity import capacity
from app.services.container import Services
from app.services.log_lines import is_noise, parse_line
from app.services.ports import host_udp_ports_in_use
from app.services.world_paths import container_name
from app.services.world_service import CreateWorldInput

router = APIRouter(prefix="/worlds")


def world_detail(services: Services, world: World) -> dict[str, Any]:
    return {
        **services.monitor.summary(world),
        "infra": {
            "bind_ip": world.bind_ip,
            "query_port": world.query_port,
            "image_repo": world.image_repo,
            "image_tag": world.image_tag,
            "puid": world.puid,
            "pgid": world.pgid,
            "game_branch": world.game_branch,
            "steamcmd_args": world.steamcmd_args,
            "use_ntsync": world.use_ntsync,
            "stop_timeout_s": world.stop_timeout_s,
            "cpu_limit": world.cpu_limit,
            "mem_limit_mb": world.mem_limit_mb,
        },
        "data_dir": str(services.worlds.paths(world).root),
        "created_at": world.created_at.isoformat(),
    }


@router.get("")
async def list_worlds(services: Services = Depends(get_services)) -> list[dict[str, Any]]:
    return [services.monitor.summary(w) for w in services.worlds.list()]


@router.get("/suggest-port")
async def suggest_port(services: Services = Depends(get_services)) -> dict[str, int]:
    return {"port": await services.worlds.suggest_port()}


def _create_input(body: WorldCreate) -> CreateWorldInput:
    return CreateWorldInput(**body.model_dump(exclude={"backup"}))


@router.post("", status_code=202)
async def create_world(
    body: WorldCreate,
    dry_run: bool = Query(False),
    services: Services = Depends(require_ready),
) -> dict:
    """Create a world (the wizard's last step). dry_run validates and returns the config
    that would be written (passwords masked) and host capacity warnings."""
    if body.backup is not None and body.backup.enabled:
        validate_cron(body.backup.cron)
    if dry_run:
        preview = await services.worlds.preview(_create_input(body))
        cap = capacity(services.settings.data_dir, len(services.worlds.list()), extra=1,
                       new_downloads=0 if body.copy_game_from else 1)
        return {**preview, "capacity": cap}
    world, job_id = await services.worlds.create(_create_input(body))
    if body.backup is not None:
        services.backups.update_schedule(world, body.backup.model_dump())
    services.monitor.notify(world.id)
    return {"world": world_detail(services, world), "job_id": job_id}


@router.get("/check-port")
async def check_port(
    port: int = Query(ge=1, le=65535),
    world_id: str | None = Query(None, description="A world that may keep using it"),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    """Is this UDP port free for a world? (other worlds, and a live check of the host)"""
    if not 1024 <= port <= 65535:
        return {"port": port, "free": False, "reason": "Use a port from 1024 to 65535"}
    other = next((w for w in services.worlds.list()
                  if w.query_port == port and w.id != world_id and w.slug != world_id), None)
    if other:
        return {"port": port, "free": False, "reason": f"Used by {other.display_name}"}
    if port in await host_udp_ports_in_use(services.docker, services.settings):
        mine = world_id and any(w.query_port == port and world_id in (w.id, w.slug)
                                for w in services.worlds.list())
        if not mine:
            return {"port": port, "free": False, "reason": "In use by another program"}
    return {"port": port, "free": True, "reason": None}


@router.get("/{world_id}")
async def get_world(world_id: str, services: Services = Depends(get_services)) -> dict:
    return world_detail(services, services.worlds.get(world_id))


@router.patch("/{world_id}")
async def patch_world(
    world_id: str, body: InfraPatch, services: Services = Depends(require_ready)
) -> dict:
    """Container settings. Saved, not applied: the response says whether the
    container must be recreated."""
    world = services.worlds.get(world_id)
    changed = await services.worlds.update_infra(world, body.model_dump(exclude_unset=True))
    world = services.worlds.get(world_id)
    await services.worlds.apply_config_if_stopped(world)
    info = await services.docker.get_container(container_name(world.slug))
    return {
        "world": world_detail(services, world),
        "changed": changed,
        "recreate_required": bool(info and services.worlds.recreate_required(world,
                                                                             info.labels)),
    }


@router.delete("/{world_id}", status_code=202)
async def delete_world(
    world_id: str,
    delete_data: bool = Query(False),
    services: Services = Depends(require_ready),
) -> JobRef:
    world = services.worlds.get(world_id)
    return JobRef(job_id=services.worlds.delete(world, delete_data))


@router.post("/{world_id}/start", status_code=202)
async def start(world_id: str, services: Services = Depends(require_ready)) -> JobRef:
    return JobRef(job_id=services.worlds.start(services.worlds.get(world_id)))


@router.post("/{world_id}/stop", status_code=202)
async def stop(
    world_id: str, body: StopRequest | None = None, services: Services = Depends(require_ready)
) -> JobRef:
    body = body or StopRequest()
    return JobRef(job_id=services.worlds.stop(services.worlds.get(world_id), body.when))


@router.post("/{world_id}/restart", status_code=202)
async def restart(
    world_id: str,
    body: RestartRequest | None = None,
    services: Services = Depends(require_ready),
) -> JobRef:
    body = body or RestartRequest()
    world = services.worlds.get(world_id)
    return JobRef(job_id=services.worlds.restart(world, body.mode, body.when))


@router.post("/{world_id}/recreate", status_code=202)
async def recreate(world_id: str, services: Services = Depends(require_ready)) -> JobRef:
    return JobRef(job_id=services.worlds.recreate(services.worlds.get(world_id)))


@router.get("/{world_id}/logs")
async def logs(
    world_id: str,
    tail: int = Query(500, ge=1, le=20000),
    services: Services = Depends(get_services),
) -> dict:
    world = services.worlds.get(world_id)
    lines = await services.docker.logs_tail(container_name(world.slug), tail=tail)
    parsed = [p for p in (parse_line(ln) for ln in lines) if not is_noise(p)]
    return {"lines": parsed}


@router.get("/{world_id}/logs/download", response_class=PlainTextResponse)
async def download_logs(
    world_id: str,
    tail: int = Query(5000, ge=1, le=100000),
    services: Services = Depends(get_services),
) -> PlainTextResponse:
    world = services.worlds.get(world_id)
    lines = await services.docker.logs_tail(container_name(world.slug), tail=tail)
    text = "\n".join(redact(ln) for ln in lines)
    return PlainTextResponse(
        text + "\n",
        headers={"Content-Disposition": f'attachment; filename="{world.slug}-logs.txt"'},
    )


@router.get("/{world_id}/events")
async def events(
    world_id: str,
    limit: int = Query(50, ge=1, le=500),
    kind: list[str] | None = Query(None),
    before: int | None = Query(None, description="Only events with a smaller id (paging)"),
    services: Services = Depends(get_services),
) -> list[dict[str, Any]]:
    world = services.worlds.get(world_id)
    return services.events.recent(world.id, limit, kinds=kind, before=before)


@router.get("/{world_id}/players")
async def players(world_id: str, services: Services = Depends(get_services)) -> dict:
    world = services.worlds.get(world_id)
    rt = services.monitor.runtime(world.id)
    return {
        "count": rt.players,
        "max": rt.max_players,
        "a2s_names": rt.player_names,
        "online": services.monitor.online_players(world.id),
    }
