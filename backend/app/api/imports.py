"""Importing a server FlameKeeper didn't create, and cleaning up the old one afterwards."""

from __future__ import annotations

import os
import shutil
from typing import Any

from fastapi import APIRouter, Depends, Query, UploadFile

from app.api.backups import CHUNK, MAX_UPLOAD_BYTES
from app.api.deps import get_services, require_ready
from app.api.worlds import world_detail
from app.schemas import ImportCreate, ImportScanRequest
from app.services.backup_service import validate_cron
from app.services.capacity import capacity
from app.services.container import Services
from app.services.world_service import WorldError

router = APIRouter()


@router.get("/imports/candidates")
async def candidates(services: Services = Depends(require_ready)) -> list[dict[str, Any]]:
    return await services.imports.candidates()


@router.post("/imports/scan")
async def scan(body: ImportScanRequest, services: Services = Depends(require_ready)
               ) -> dict[str, Any]:
    """Find the savegame and config of a container or host folder, and copy them for a
    preview. Nothing about the source changes."""
    if body.kind == "container":
        if not body.container:
            raise WorldError("Choose a container", 422)
        return await services.imports.scan_container(body.container)
    if not body.path:
        raise WorldError("Enter the server's folder", 422)
    return await services.imports.scan_host_path(body.path)


@router.post("/imports/upload")
async def upload(file: UploadFile, services: Services = Depends(require_ready)
                 ) -> dict[str, Any]:
    token, dest = services.imports.upload_path()
    written = 0
    try:
        with open(dest, "wb") as out:
            os.chmod(dest, 0o600)
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise WorldError("The file is larger than 20 GB", 413)
                out.write(chunk)
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise
    return await services.imports.scan_upload(token, file.filename or "upload.zip")


@router.get("/imports/{token}")
async def get_scan(token: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    return services.imports.load(token)


@router.post("/imports/{token}/create", status_code=202)
async def create(token: str, body: ImportCreate, dry_run: bool = Query(False),
                 services: Services = Depends(require_ready)) -> dict[str, Any]:
    """Create a world from the scanned server. A running container is stopped (and its
    restart policy set to "no") just before the new world's first start."""
    if body.backup is not None and body.backup.enabled:
        validate_cron(body.backup.cron)
    data = body.model_dump(exclude={"backup"})
    if dry_run:
        scan = await services.imports.check(token, data)
        preview = await services.worlds.preview(await services.imports.create_input(scan, data))
        cap = capacity(services.settings.data_dir, len(services.worlds.list()), extra=1,
                       new_downloads=0 if body.copy_game_from or body.copy_game_from_source
                       else 1)
        return {**preview, "capacity": cap}
    world, job_id = await services.imports.create(token, data)
    if body.backup is not None:
        services.backups.update_schedule(world, body.backup.model_dump())
    services.monitor.notify(world.id)
    return {"world": world_detail(services, world), "job_id": job_id}


@router.get("/worlds/{world_id}/import")
async def import_info(world_id: str, services: Services = Depends(get_services)
                      ) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return {"import": await services.imports.info(world)}


@router.post("/worlds/{world_id}/import/remove-container")
async def remove_container(world_id: str, services: Services = Depends(require_ready)
                           ) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return {"import": await services.imports.remove_container(world)}


@router.post("/worlds/{world_id}/import/remove-volumes")
async def remove_volumes(world_id: str, services: Services = Depends(require_ready)
                         ) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return await services.imports.remove_volumes(world)


@router.post("/worlds/{world_id}/import/dismiss", status_code=204)
async def dismiss(world_id: str, services: Services = Depends(require_ready)) -> None:
    world = services.worlds.get(world_id)
    await services.imports.dismiss(world)
