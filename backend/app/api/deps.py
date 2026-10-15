"""Shared request dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.services.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


async def current_user(request: Request) -> None:
    """Auth seam. v1 has no authentication and assumes a trusted network.

    Add authentication here (OIDC, forward-auth header, local users); every API router
    depends on this function.
    """
    return None


def require_ready(services: Services = Depends(get_services)) -> Services:
    """Block world-changing actions when the startup self-check found a fatal problem."""
    if services.selfcheck and services.selfcheck.fatal_error:
        raise HTTPException(503, f"Setup problem: {services.selfcheck.fatal_error}")
    return services
