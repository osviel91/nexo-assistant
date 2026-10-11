"""Unmounted FastAPI auth contract for isolated tests; not part of app.main."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from prototypes.admin_auth import AdminAuthStore, AuthFailure, Principal


COOKIE_NAME = "__Host-nexo_session"


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: SecretStr


def _authority(scheme: str, netloc: str) -> tuple[str, int | None]:
    parsed = urlsplit(f"{scheme}://{netloc}")
    if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid authority")
    port = parsed.port
    return parsed.hostname.lower(), port or (443 if scheme == "https" else 80)


def validate_browser_request(request: Request, allowed_origin: str, *, require_origin: bool) -> None:
    try:
        allowed = urlsplit(allowed_origin)
        if allowed.scheme != "https" or not allowed.hostname or allowed.username or allowed.password or allowed.path not in ("", "/") or allowed.query or allowed.fragment:
            raise ValueError
        allowed_authority = _authority("https", allowed.netloc)
        request_authority = _authority("https", request.headers.get("host", ""))
        if request_authority != allowed_authority or request.url.scheme != "https":
            raise ValueError
        origin = request.headers.get("origin")
        if require_origin and not origin:
            raise ValueError
        if origin is not None:
            parsed_origin = urlsplit(origin)
            if parsed_origin.scheme != "https" or _authority("https", parsed_origin.netloc) != allowed_authority or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
                raise ValueError
    except ValueError:
        raise HTTPException(400, "request_origin_rejected") from None


def _raise_http(error: AuthFailure) -> None:
    raise HTTPException(error.status_code, error.code) from None


def require_permission(auth: AdminAuthStore, allowed_origin: str, permission: str, *, mutating: bool = False):
    def dependency(request: Request) -> Principal:
        validate_browser_request(request, allowed_origin, require_origin=mutating)
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication_required")
        try:
            return auth.authenticate(token, permission, csrf_token=request.headers.get("x-csrf-token"),
                                    mutating=mutating)
        except AuthFailure as error:
            _raise_http(error)
    return dependency


def create_auth_router(auth: AdminAuthStore, allowed_origin: str) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["authentication"])

    @router.post("/login")
    def login(item: LoginInput, request: Request, response: Response):
        validate_browser_request(request, allowed_origin, require_origin=True)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        try:
            session = auth.login(item.username, item.password.get_secret_value())
        except AuthFailure as error:
            _raise_http(error)
        response.set_cookie(COOKIE_NAME, session.token, max_age=auth.absolute_ttl,
                            secure=True, httponly=True, samesite="lax", path="/")
        return {"authenticated": True, "username": item.username, "csrf_token": session.csrf_token}

    @router.get("/session")
    def session(request: Request, response: Response):
        validate_browser_request(request, allowed_origin, require_origin=False)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication_required")
        try:
            principal = auth.authenticate(token, "config:read")
        except AuthFailure as error:
            _raise_http(error)
        return {"authenticated": True, "username": principal.subject,
                "permissions": sorted(principal.permissions)}

    @router.post("/csrf")
    def csrf_token(request: Request, response: Response):
        validate_browser_request(request, allowed_origin, require_origin=True)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication_required")
        try:
            csrf = auth.issue_csrf_token(token)
        except AuthFailure as error:
            _raise_http(error)
        return {"csrf_token": csrf}

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response):
        validate_browser_request(request, allowed_origin, require_origin=True)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        token = request.cookies.get(COOKIE_NAME)
        csrf = request.headers.get("x-csrf-token")
        if not token:
            raise HTTPException(401, "authentication_required")
        try:
            auth.authenticate(token, "config:read", csrf_token=csrf, mutating=True)
            auth.logout(token)
        except AuthFailure as error:
            _raise_http(error)
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
        return Response(status_code=204, headers=response.headers)

    return router
