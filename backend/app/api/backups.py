"""Backups, restore, clone and backup schedules."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_services, require_ready
from app.api.worlds import world_detail
from app.schemas import (
    BackupCreate,
    BackupNote,
    BackupScheduleUpdate,
    CloneRequest,
    JobRef,
    RestoreRequest,
)
from app.services.backup_archive import latest_mtime
from app.services.container import Services
from app.services.world_service import WorldError

log = logging.getLogger(__name__)


def _iso(dt):
    return dt.isoformat() if dt else None
router = APIRouter(prefix="/worlds/{world_id}")

MAX_UPLOAD_BYTES = 20 * 1024**3
CHUNK = 1024 * 1024


@router.get("/backups")
async def list_backups(
    world_id: str,
    refresh_disk: bool = Query(False),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return {
        "backups": services.backups.list(world),
        "schedule": services.backups.schedule(world),
        "disk": services.backups.disk(world, refresh=refresh_disk),
        "savegame_saved_at": _iso(latest_mtime(services.worlds.paths(world).savegame)),
    }


@router.post("/backups", status_code=202)
async def create_backup(
    world_id: str, body: BackupCreate | None = None, services: Services = Depends(require_ready)
) -> JobRef:
    body = body or BackupCreate()
    world = services.worlds.get(world_id)
    return JobRef(job_id=services.backups.create(world, kind="manual", note=body.note,
                                                 mode=body.mode))


@router.get("/backups/{backup_id}/download")
async def download_backup(
    world_id: str, backup_id: str, services: Services = Depends(get_services)
) -> FileResponse:
    world = services.worlds.get(world_id)
    b = services.backups.get(world, backup_id)
    log.info("backup downloaded", extra={"data": {"world": world.slug, "file": b.filename}})
    path = services.backups.file(world, b)
    return FileResponse(path, media_type="application/zip",
                        filename=f"{world.slug}-{path.name}")


@router.get("/backups/{backup_id}")
async def inspect_backup(
    world_id: str, backup_id: str, services: Services = Depends(get_services)
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return services.backups.inspect(world, backup_id)


@router.post("/backups/upload", status_code=201)
async def upload_backup(
    world_id: str,
    file: UploadFile,
    request: Request,
    services: Services = Depends(require_ready),
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    folder = services.worlds.paths(world).backups
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".zip", dir=folder)
    tmp = Path(tmp_name)
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise WorldError("The file is larger than 20 GB", 413)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return await services.backups.add_upload(world, tmp, file.filename or "upload.zip")


@router.patch("/backups/{backup_id}")
async def set_note(
    world_id: str, backup_id: str, body: BackupNote, services: Services = Depends(get_services)
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return services.backups.set_note(world, backup_id, body.note)


@router.delete("/backups/{backup_id}", status_code=204)
async def delete_backup(
    world_id: str, backup_id: str, services: Services = Depends(require_ready)
) -> None:
    world = services.worlds.get(world_id)
    services.backups.delete(world, backup_id)


@router.post("/backups/{backup_id}/restore", status_code=202)
async def restore_backup(
    world_id: str,
    backup_id: str,
    body: RestoreRequest | None = None,
    services: Services = Depends(require_ready),
) -> JobRef:
    body = body or RestoreRequest()
    world = services.worlds.get(world_id)
    return JobRef(job_id=services.backups.restore(world, backup_id,
                                                  restore_config=body.restore_config,
                                                  when=body.when))


@router.post("/clone", status_code=202)
async def clone_world(
    world_id: str, body: CloneRequest, services: Services = Depends(require_ready)
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    new, job_id = await services.backups.clone(
        world, source=body.source, display_name=body.display_name, slug=body.slug,
        query_port=body.query_port)
    services.monitor.notify(new.id)
    return {"world": world_detail(services, new), "job_id": job_id}


@router.get("/schedules")
async def get_schedules(world_id: str, services: Services = Depends(get_services)) -> list:
    world = services.worlds.get(world_id)
    return [services.backups.schedule(world)]


@router.put("/schedules/backup")
async def put_backup_schedule(
    world_id: str, body: BackupScheduleUpdate, services: Services = Depends(require_ready)
) -> dict[str, Any]:
    world = services.worlds.get(world_id)
    return services.backups.update_schedule(world, body.model_dump(exclude_none=True))
