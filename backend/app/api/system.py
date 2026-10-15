"""Health, host info, settings schema, self-check."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.api.deps import get_services
from app.config import APP_NAME
from app.services.app_settings import SettingsError
from app.services.app_update import AppUpdateError
from app.services.capacity import capacity
from app.services.container import Services
from app.services.notifications import NotificationError
from app.services.selfcheck import run_selfcheck
from app.services.server_readme import README_NAME, compare, parse_readme
from app.services.settings_schema import load_schema, schema_bytes

router = APIRouter()


@router.get("/health")
async def health(services: Services = Depends(get_services)) -> dict:
    sc = services.selfcheck
    return {"status": "ok", "app": APP_NAME, "setup_error": sc.fatal_error if sc else None}


def _meminfo_total() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


@router.get("/system")
async def system(services: Services = Depends(get_services)) -> dict:
    s = services.settings
    usage = shutil.disk_usage(s.data_dir) if s.data_dir.exists() else None
    sc = services.selfcheck
    return {
        "app": APP_NAME,
        "version": s.flamekeeper_version,
        "data_dir": str(s.data_dir),
        "base_path": s.base_path or "/",
        "in_container": s.in_container,
        "fake_docker": s.fake_docker,
        "a2s_mode": s.resolved_a2s_mode,
        "host": {
            "cpus": os.cpu_count(),
            "mem_total": _meminfo_total(),
            "kernel": platform.release(),
            "disk_total": usage.total if usage else None,
            "disk_free": usage.free if usage else None,
        },
        "selfcheck": sc.to_dict() if sc else None,
        "ntsync_available": bool(sc and sc.ntsync_available),
        "docker_version": sc.docker_version if sc else None,
        "auth": "none",
    }


@router.post("/system/selfcheck")
async def rerun_selfcheck(services: Services = Depends(get_services)) -> dict:
    services.selfcheck = await run_selfcheck(services.settings, services.docker)
    services.worlds.ntsync_available = services.selfcheck.ntsync_available
    return services.selfcheck.to_dict()


@router.get("/settings-schema")
async def settings_schema() -> Response:
    return Response(schema_bytes(), media_type="application/json")


@router.get("/system/settings-readme")
async def settings_readme(services: Services = Depends(get_services)) -> dict:
    """Keen's enshrouded_server_readme.txt from each world's install, compared with the
    settings schema. Worlds on the same game build share one entry."""
    readmes: dict[str, dict] = {}
    missing: list[str] = []
    for world in services.worlds.list():
        server_dir = services.worlds.paths(world).server
        try:
            raw = (server_dir / README_NAME).read_bytes()
        except FileNotFoundError:
            missing.append(world.slug)
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if sha not in readmes:
            readme = parse_readme(raw.decode("utf-8", errors="replace"))
            readmes[sha] = {"version": readme.version, "sha": sha, "worlds": [],
                            "settings": len(readme.settings),
                            "drift": [d.as_dict() for d in compare(readme)]}
        readmes[sha]["worlds"].append(world.slug)
    return {"readmes": list(readmes.values()), "not_installed": missing,
            "schema_settings": len(load_schema()["settings"])}


@router.get("/system/capacity")
async def system_capacity(
    extra: int = Query(0, ge=0, le=20), services: Services = Depends(get_services)
) -> dict:
    """Host resources vs Keen's guidance for the current worlds (+ `extra` planned)."""
    return capacity(services.settings.data_dir, len(services.worlds.list()), extra,
                    new_downloads=extra)


class NotificationSettings(BaseModel):
    discord_webhook: str | None = None  # None keeps the saved one; "" removes it
    events: list[str] | None = None


class NotificationTest(BaseModel):
    discord_webhook: str | None = None  # test a URL before saving it


@router.get("/settings/notifications")
async def get_notifications(services: Services = Depends(get_services)) -> dict:
    return services.notifier.view()


@router.put("/settings/notifications")
async def put_notifications(body: NotificationSettings,
                            services: Services = Depends(get_services)) -> dict:
    try:
        return services.notifier.update(discord_webhook=body.discord_webhook,
                                        events=body.events)
    except NotificationError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/settings/notifications/test", status_code=204)
async def test_notifications(body: NotificationTest,
                             services: Services = Depends(get_services)) -> None:
    try:
        await services.notifier.test(body.discord_webhook)
    except NotificationError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/settings/app")
async def get_app_settings(services: Services = Depends(get_services)) -> dict:
    return services.app_settings.view()


@router.put("/settings/app")
async def put_app_settings(
    body: dict[str, Any], services: Services = Depends(get_services)
) -> dict:
    try:
        return services.app_settings.update(body)
    except SettingsError as e:
        raise HTTPException(422, str(e)) from e


class AppUpdateRequest(BaseModel):
    version: str


@router.get("/system/update")
async def get_app_update(services: Services = Depends(get_services)) -> dict:
    return services.app_update.status()


@router.post("/system/update/check")
async def check_app_update(services: Services = Depends(get_services)) -> dict:
    return await services.app_update.check(manual=True)


@router.post("/system/update", status_code=202)
async def start_app_update(body: AppUpdateRequest,
                           services: Services = Depends(get_services)) -> dict:
    try:
        return await services.app_update.start_update(body.version)
    except AppUpdateError as e:
        raise HTTPException(e.status, str(e)) from e
