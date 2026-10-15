"""Shared request dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.services.auth import COOKIE_NAME, CurrentUser
from app.services.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


# Reachable without signing in: the installer and Docker health checks poll it.
PUBLIC_PATHS = ("/api/health",)


async def current_user(request: Request) -> CurrentUser | None:
    """Every API router depends on this: the signed-in admin, or 401."""
    services: Services = request.app.state.services
    base = services.settings.base_path
    if any(request.url.path == f"{base}{p}" for p in PUBLIC_PATHS):
        return None
    user = services.auth.session_user(request.cookies.get(COOKIE_NAME))
    if user is None:
        setup = services.auth.setup_required()
        raise HTTPException(401, "Set up FlameKeeper first" if setup else "Sign in first")
    request.state.user = user
    return user


def require_ready(services: Services = Depends(get_services)) -> Services:
    """Block world-changing actions when the startup self-check found a fatal problem."""
    if services.selfcheck and services.selfcheck.fatal_error:
        raise HTTPException(503, f"Setup problem: {services.selfcheck.fatal_error}")
    return services
