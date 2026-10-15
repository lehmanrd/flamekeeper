"""Sign-in, first setup and the admin's password. Mounted outside the signed-in API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.deps import current_user, get_services
from app.services.auth import COOKIE_NAME, SESSION_DAYS, AuthError, CurrentUser
from app.services.container import Services

router = APIRouter(prefix="/auth", tags=["auth"])


class SetupBody(BaseModel):
    username: str
    password: str


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


def _raise(e: AuthError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


def _set_cookie(response: Response, request: Request, token: str, services: Services) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=_secure(request),
                        path=f"{services.settings.base_path}/" or "/")


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/status")
async def status(request: Request, services: Services = Depends(get_services)) -> dict:
    user = services.auth.session_user(request.cookies.get(COOKIE_NAME))
    required = services.auth.setup_required()
    return {"setup_required": required,
            "setup_locked": required and not services.auth.setup_open(),
            "user": {"username": user.username} if user else None}


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response,
                services: Services = Depends(get_services)) -> dict:
    try:
        user = services.auth.create_admin(body.username, body.password)
    except AuthError as e:
        raise _raise(e) from e
    _set_cookie(response, request, services.auth.start_session(user), services)
    return {"user": {"username": user.username}}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response,
                services: Services = Depends(get_services)) -> dict:
    if services.auth.setup_required():
        raise HTTPException(409, "Set up FlameKeeper first")
    try:
        user = services.auth.login(body.username, body.password, _client(request))
    except AuthError as e:
        raise _raise(e) from e
    _set_cookie(response, request, services.auth.start_session(user), services)
    return {"user": {"username": user.username}}


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response,
                 services: Services = Depends(get_services)) -> None:
    services.auth.end_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path=f"{services.settings.base_path}/" or "/")


@router.post("/password", status_code=204)
async def change_password(body: PasswordBody, request: Request,
                          user: CurrentUser = Depends(current_user),
                          services: Services = Depends(get_services)) -> None:
    """Change the admin's password; other signed-in browsers are signed out."""
    try:
        services.auth.change_password(user, body.current_password, body.new_password)
    except AuthError as e:
        raise _raise(e) from e
    services.auth.end_other_sessions(user, request.cookies.get(COOKIE_NAME))
