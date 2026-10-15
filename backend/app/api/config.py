"""Server config editor API: settings, roles, revisions, import/export."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.api.deps import get_services, require_ready
from app.schemas import ApplyRequest, BaseRevision, ConfigImport, ConfigPut, JobRef
from app.services.config_service import ConfigUpdate, SaveResult
from app.services.container import Services
from app.services.world_paths import container_name

log = logging.getLogger(__name__)
router = APIRouter(prefix="/worlds/{world_id}")


async def _saved(services: Services, world_id: str, result: SaveResult) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    await services.worlds.apply_config_if_stopped(world)
    services.monitor.notify(world.id)
    return {
        "revision": result.revision,
        "changes": result.changes,
        "warnings": result.warnings,
        "config": services.config.view(world),
    }


@router.get("/config")
async def get_config(world_id: str, services: Services = Depends(get_services)) -> dict:
    return services.config.view(services.worlds.get(world_id))


@router.put("/config")
async def put_config(
    world_id: str,
    body: ConfigPut,
    dry_run: bool = Query(False),
    services: Services = Depends(require_ready),
) -> dict:
    world = services.worlds.get(world_id)
    update = ConfigUpdate(
        base_revision=body.base_revision,
        preset=body.preset,
        game_settings=body.gameSettings,
        server=body.server,
        roles=body.roles,
    )
    result = services.config.save(world, update, note=body.note, dry_run=dry_run)
    if dry_run:
        return {"revision": result.revision, "changes": result.changes,
                "warnings": result.warnings}
    return await _saved(services, world.id, result)


@router.post("/config/apply", status_code=202)
async def apply_config(
    world_id: str,
    body: ApplyRequest | None = None,
    services: Services = Depends(require_ready),
) -> JobRef:
    """Restart the server process so it reads the latest saved revision."""
    world = services.worlds.get(world_id)
    info = await services.docker.get_container(container_name(world.slug))
    if info is None or not info.running:
        raise HTTPException(409, "The world is not running; saved changes apply when it starts")
    when = (body or ApplyRequest()).when
    return JobRef(job_id=services.worlds.restart(world, "process", when))


@router.get("/config/revisions")
async def list_revisions(world_id: str, services: Services = Depends(get_services)) -> list:
    return services.config.revisions(services.worlds.get(world_id))


@router.get("/config/revisions/{revision}")
async def get_revision(
    world_id: str, revision: int, services: Services = Depends(get_services)
) -> dict:
    return services.config.revision(services.worlds.get(world_id), revision)


@router.post("/config/revisions/{revision}/restore")
async def restore_revision(
    world_id: str,
    revision: int,
    body: BaseRevision | None = None,
    services: Services = Depends(require_ready),
) -> dict:
    world = services.worlds.get(world_id)
    result = services.config.restore(world, revision, (body or BaseRevision()).base_revision)
    return await _saved(services, world.id, result)


@router.get("/config/export")
async def export_config(
    world_id: str,
    include_passwords: bool = Query(False),
    services: Services = Depends(get_services),
) -> Response:
    world = services.worlds.get(world_id)
    doc = services.config.export(world, include_passwords)
    if include_passwords:
        log.info("config exported with passwords", extra={"data": {"world": world.slug}})
    suffix = "" if include_passwords else "-masked"
    return Response(
        json.dumps(doc, indent="\t", ensure_ascii=False) + "\n",
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{world.slug}-enshrouded_server{suffix}.json"'},
    )


@router.post("/config/import")
async def import_config(
    world_id: str,
    body: ConfigImport,
    dry_run: bool = Query(True),
    services: Services = Depends(require_ready),
) -> dict:
    """Replace settings, server fields and roles from a file. dry_run (default) only
    returns the diff and warnings."""
    world = services.worlds.get(world_id)
    result = services.config.import_document(
        world, body.document, base_revision=body.base_revision,
        convert_legacy_password=body.convert_legacy_password, dry_run=dry_run)
    if dry_run:
        return {"revision": result.revision, "changes": result.changes,
                "warnings": result.warnings}
    return await _saved(services, world.id, result)


@router.post("/config/legacy-password/convert")
async def convert_legacy_password(
    world_id: str,
    body: BaseRevision | None = None,
    services: Services = Depends(require_ready),
) -> dict:
    world = services.worlds.get(world_id)
    result = services.config.convert_legacy_password(world, (body or BaseRevision()).base_revision)
    return await _saved(services, world.id, result)


@router.post("/roles/{index}/reveal")
async def reveal_password(
    world_id: str, index: int, services: Services = Depends(get_services)
) -> dict:
    world = services.worlds.get(world_id)
    role = services.config.reveal(world, index)
    log.info("role password revealed", extra={"data": {"world": world.slug,
                                                        "role": role["name"]}})
    return role
