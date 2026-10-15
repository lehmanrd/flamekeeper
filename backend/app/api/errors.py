"""RFC 7807 problem+json error responses."""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.config_service import ConfigError
from app.services.docker_adapter import DockerError
from app.services.world_service import WorldError

log = logging.getLogger(__name__)
PROBLEM = "application/problem+json"


def problem(status: int, detail: str, request: Request, **extra: object) -> JSONResponse:
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail,
            "instance": request.url.path, **extra}
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        detail = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in errors)
        return problem(422, detail or "Invalid request", request, errors=errors)

    @app.exception_handler(WorldError)
    async def _world(request: Request, exc: WorldError) -> JSONResponse:
        return problem(exc.status, str(exc), request)

    @app.exception_handler(ConfigError)
    async def _config(request: Request, exc: ConfigError) -> JSONResponse:
        return problem(exc.status, str(exc), request, errors=exc.errors)

    @app.exception_handler(DockerError)
    async def _docker(request: Request, exc: DockerError) -> JSONResponse:
        return problem(502, f"Docker: {exc}", request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return problem(500, "Internal error; see controller logs", request)
