"""FlameKeeper controller: FastAPI app factory and startup sequence."""

from __future__ import annotations

import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import backups as backups_api
from app.api import config as config_api
from app.api import jobs as jobs_api
from app.api import stream as stream_api
from app.api import system as system_api
from app.api import updates as updates_api
from app.api import worlds as worlds_api
from app.api.deps import current_user
from app.api.errors import install_error_handlers
from app.config import APP_NAME, Settings, get_settings
from app.db import init_engine, run_migrations
from app.logging_setup import setup_logging
from app.services.container import Services
from app.services.docker_adapter import DockerAdapter
from app.services.selfcheck import run_selfcheck

log = logging.getLogger("flamekeeper")


async def _join_game_network(services: Services) -> None:
    """Attach the controller to the game network so A2S can reach containers by name."""
    s = services.settings
    services.worlds.network = s.docker_network
    try:
        await services.docker.ensure_network(s.docker_network)
    except Exception as e:
        log.warning("could not create docker network %s: %s", s.docker_network, e)
        services.worlds.network = None
    if s.resolved_a2s_mode != "network" or s.fake_docker:
        return
    try:
        await services.docker.connect_network(s.docker_network, socket.gethostname())
    except Exception as e:
        log.warning("could not join network %s (%s); falling back to A2S via %s",
                    s.docker_network, e, s.a2s_host)
        s.a2s_mode = "host"


async def startup(services: Services) -> None:
    services.jobs.recover_interrupted()
    services.selfcheck = await run_selfcheck(services.settings, services.docker)
    for check in services.selfcheck.checks:
        level = logging.ERROR if check.status == "fail" else logging.INFO
        log.log(level, "self-check %s: %s - %s", check.id, check.status, check.detail)
    if services.selfcheck.fatal_error:
        log.error("SETUP PROBLEM: %s", services.selfcheck.fatal_error)
        return
    services.worlds.ntsync_available = services.selfcheck.ntsync_available
    await _join_game_network(services)
    await services.worlds.reconcile()
    services.monitor.start()
    services.scheduler.start()


def create_app(
    settings: Settings | None = None,
    docker: DockerAdapter | None = None,
    poll_interval: float | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        run_migrations(settings.db_url)
        init_engine(settings.db_url)
        adapter = docker
        if adapter is None:
            if settings.fake_docker:
                from app.services.docker_fake import FakeDockerAdapter

                adapter = FakeDockerAdapter(boot_delay=3)
            else:
                from app.services.docker_adapter import RealDockerAdapter

                adapter = RealDockerAdapter()
        services = Services.build(settings, adapter, poll_interval)
        services.app_settings.load()
        app.state.services = services
        await startup(services)
        log.info("%s ready on base path %r", APP_NAME, settings.base_path or "/")
        try:
            yield
        finally:
            # Game containers keep running: the controller only stops its own tasks.
            await services.scheduler.stop()
            await services.monitor.stop()
            await services.jobs.shutdown()

    app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=f"{settings.base_path}/api/openapi.json")
    install_error_handlers(app)

    api = APIRouter(prefix=f"{settings.base_path}/api", dependencies=[Depends(current_user)])
    for module in (system_api, worlds_api, config_api, backups_api, updates_api, jobs_api,
                   stream_api):
        api.include_router(module.router)
    app.include_router(api)
    _mount_spa(app, settings)
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    static = settings.resolved_static_dir
    base = settings.base_path
    if static is None or not (static / "index.html").exists():
        return
    # The SPA is built with relative asset URLs; a <base> tag makes them resolve under
    # BASE_PATH on every route, and the router reads its basename from __FK_BASE__.
    index_html = (static / "index.html").read_text().replace(
        "<head>",
        f'<head><base href="{base}/"><script>window.__FK_BASE__ = "{base}";</script>',
        1,
    )
    if (static / "assets").exists():
        app.mount(f"{base}/assets", StaticFiles(directory=static / "assets"), name="assets")

    if base:
        @app.get("/", include_in_schema=False)
        async def _root_redirect() -> RedirectResponse:
            return RedirectResponse(f"{base}/")

    @app.get(f"{base}/{{path:path}}", include_in_schema=False)
    async def _spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (static / path).resolve()
        if path and candidate.is_file() and Path(static).resolve() in candidate.parents:
            return FileResponse(candidate)
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})


def get_app() -> FastAPI:
    """Uvicorn factory entry point: `uvicorn app.main:get_app --factory`."""
    return create_app()
