"""Isolated fail-closed ASGI authorization guard prototype; never mount in NEXO."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount

from prototypes.admin_auth import AuthFailure, Principal
from prototypes.http_policy_inventory import (
    AccessPolicy,
    PolicyKind,
    assert_snapshot_current,
    inspect_routes,
)


COOKIE_NAME = "__Host-nexo_session"
AdminVerifier = Callable[[Request, Principal], bool]
OwnershipResolver = Callable[[Request, Any, AccessPolicy], bool]


@dataclass(frozen=True)
class GuardDecision:
    principal: Principal | None
    policy: AccessPolicy
    resource_id: str | None = None


@dataclass(frozen=True)
class ServiceCredential:
    principal: Principal
    audience: str
    scopes: frozenset[str]
    owner_user_id: str | None = None
    delegation_scopes: frozenset[str] = frozenset()
    expires_at: float | None = None
    revoked: bool = False


SERVICE_VERIFIER = Callable[[Request], ServiceCredential | None]


class GuardNotReady(RuntimeError):
    pass


class HttpAuthorizationGuard:
    """ASGI gate. Call validate() after all route and module registration."""

    def __init__(self, app: Any, auth: Any, *, allowed_origin: str,
                 service_verifier: SERVICE_VERIFIER | None = None,
                 admin_verifier: AdminVerifier | None = None,
                 ownership_resolver: OwnershipResolver | None = None) -> None:
        self.app = app
        self.auth = auth
        self.allowed_origin = allowed_origin
        self.service_verifier = service_verifier
        self.admin_verifier = admin_verifier
        self.ownership_resolver = ownership_resolver
        self.snapshot = None

    def validate(self) -> None:
        self.snapshot = inspect_routes(self.app, strict=True)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            if self.snapshot is None:
                raise GuardNotReady("policy inventory has not been validated")
            assert_snapshot_current(self.app, self.snapshot)
            request = Request(scope, receive=receive)
            policies = _matching_policies(self.app, scope)
            if not policies:
                if scope.get("path", "").startswith("/api/"):
                    await JSONResponse({"detail": "not_found"}, status_code=404)(scope, receive, send)
                else:
                    await self.app(scope, receive, send)
                return
            decision = self._authorize(request, policies)
            scope.setdefault("state", {})["authorization"] = decision
        except GuardNotReady:
            await JSONResponse({"detail": "authorization_unavailable"}, status_code=503)(scope, receive, send)
            return
        except AuthFailure as error:
            await JSONResponse({"detail": error.code}, status_code=error.status_code)(scope, receive, send)
            return
        except Exception:
            # Storage, verifier, and ownership failures are deliberately indistinguishable.
            await JSONResponse({"detail": "authorization_unavailable"}, status_code=503)(scope, receive, send)
            return
        stream_checked = False
        stream_blocked = False

        async def guarded_send(message: dict) -> None:
            nonlocal stream_checked, stream_blocked
            if stream_blocked:
                return
            if message["type"] == "http.response.start" and not stream_checked:
                stream_checked = True
                content_type = dict(message.get("headers", ())).get(b"content-type", b"").lower()
                if b"text/event-stream" in content_type:
                    try:
                        self._authorize(request, policies)
                    except AuthFailure as error:
                        stream_blocked = True
                        await JSONResponse({"detail": error.code}, status_code=error.status_code)(
                            scope, receive, send,
                        )
                        return
                    except Exception:
                        stream_blocked = True
                        await JSONResponse({"detail": "authorization_unavailable"}, status_code=503)(
                            scope, receive, send,
                        )
                        return
            await send(message)

        await self.app(scope, receive, guarded_send)

    def _authorize(self, request: Request, policies: list[tuple[str, AccessPolicy]]) -> GuardDecision:
        principal: Principal | None = None
        selected = policies[-1][1]
        for method, policy in policies:
            if policy.kind == PolicyKind.PUBLIC:
                continue
            if policy.kind == PolicyKind.SERVICE:
                if self.service_verifier is None:
                    raise AuthFailure("authentication_required", 401)
                result = self.service_verifier(request)
                if result is None:
                    raise AuthFailure("authentication_required", 401)
                principal = result.principal
                delegated = result.owner_user_id is not None
                if (principal.kind != "service" or policy.scope not in principal.permissions
                        or not result.scopes.issubset(principal.permissions)
                        or policy.scope not in result.scopes or result.audience != policy.audience
                        or result.revoked
                        or delegated and (not result.expires_at or result.expires_at <= time.time()
                            or not result.delegation_scopes.issubset(result.scopes)
                            or policy.scope not in result.delegation_scopes
                            or self.ownership_resolver is None
                            or not self.ownership_resolver(request, result, policy))):
                    raise AuthFailure("permission_denied", 403)
                continue

            token = request.cookies.get(COOKIE_NAME)
            if not token:
                raise AuthFailure("authentication_required", 401)
            mutating = method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            _validate_browser_request(request, self.allowed_origin, require_origin=mutating)
            if mutating:
                _validate_content_type(request)
            principal = self.auth.authenticate(
                token, policy.permission, csrf_token=request.headers.get("x-csrf-token"),
                mutating=mutating,
            )
            if principal.kind != "user":
                raise AuthFailure("permission_denied", 403)
            if policy.kind == PolicyKind.ADMIN and (self.admin_verifier is None
                    or not self.admin_verifier(request, principal)):
                raise AuthFailure("permission_denied", 403)
            if policy.permission not in principal.permissions:
                raise AuthFailure("permission_denied", 403)
            if self.ownership_resolver is None:
                raise AuthFailure("authorization_unavailable", 503)
            if not self.ownership_resolver(request, principal, policy):
                raise AuthFailure("resource_not_found", 404)
        return GuardDecision(principal, selected)

    def authorize_effect(self, request: Request, policy: AccessPolicy) -> Principal:
        """Recheck a protected effect during a long-running producer."""
        if policy.kind != PolicyKind.USER or not policy.permission:
            raise AuthFailure("permission_denied", 403)
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise AuthFailure("authentication_required", 401)
        try:
            principal = self.auth.authenticate(token, policy.permission)
        except AuthFailure:
            raise
        except Exception:
            raise AuthFailure("authorization_unavailable", 503) from None
        if principal.kind != "user" or policy.permission not in principal.permissions:
            raise AuthFailure("permission_denied", 403)
        if self.ownership_resolver is None:
            raise AuthFailure("authorization_unavailable", 503)
        try:
            owned = self.ownership_resolver(request, principal, policy)
        except Exception:
            raise AuthFailure("authorization_unavailable", 503) from None
        if not owned:
            raise AuthFailure("resource_not_found", 404)
        return principal

    def authorize_service_effect(self, request: Request, policy: AccessPolicy) -> ServiceCredential:
        if policy.kind != PolicyKind.SERVICE or self.service_verifier is None:
            raise AuthFailure("permission_denied", 403)
        try:
            credential = self.service_verifier(request)
        except Exception:
            raise AuthFailure("authorization_unavailable", 503) from None
        if credential is None:
            raise AuthFailure("authentication_required", 401)
        if (credential.revoked or credential.audience != policy.audience
                or policy.scope not in credential.scopes
                or not credential.scopes.issubset(credential.principal.permissions)
                or policy.scope not in credential.principal.permissions
                or credential.principal.kind != "service"):
            raise AuthFailure("permission_denied", 403)
        if credential.owner_user_id is not None and (
                not credential.expires_at or credential.expires_at <= time.time()
                or not credential.delegation_scopes.issubset(credential.scopes)
                or policy.scope not in credential.delegation_scopes
                or self.ownership_resolver is None):
            raise AuthFailure("permission_denied", 403)
        if credential.owner_user_id is not None:
            try:
                owned = self.ownership_resolver(request, credential, policy)
            except Exception:
                raise AuthFailure("authorization_unavailable", 503) from None
            if not owned:
                raise AuthFailure("resource_not_found", 404)
        return credential


def _matching_policies(app: Any, scope: dict) -> list[tuple[str, AccessPolicy]]:
    found: list[tuple[str, AccessPolicy]] = []

    def walk(routes: list[Any], current_scope: dict) -> bool:
        for route in routes:
            match, child_scope = route.matches(current_scope)
            if match == Match.NONE:
                continue
            if isinstance(route, Mount):
                declared = getattr(route, "__http_access_policies__", {})
                policy = declared.get("MOUNT")
                if policy is None:
                    return True
                found.append(("MOUNT", policy))
                nested = getattr(route.app, "routes", None)
                if nested is not None and walk(list(nested), child_scope):
                    return True
                return True
            endpoint = getattr(route, "endpoint", route)
            route_path = getattr(route, "path", "")
            if (route_path == "/{path:path}" and current_scope.get("path", "").startswith("/api/")):
                return True
            method = current_scope.get("method", "GET").upper()
            declared = getattr(endpoint, "__http_access_policies__", {})
            policy = declared.get(method)
            if policy is not None:
                found.append((method, policy))
            return True
        return False

    walk(list(app.routes), scope)
    return found


def _validate_browser_request(request: Request, allowed_origin: str, *, require_origin: bool) -> None:
    try:
        allowed = urlsplit(allowed_origin)
        if (allowed.scheme != "https" or not allowed.hostname or allowed.username or allowed.password
                or allowed.path not in ("", "/") or allowed.query or allowed.fragment):
            raise ValueError
        expected = _authority(allowed.netloc)
        if request.url.scheme != "https" or _authority(request.headers.get("host", "")) != expected:
            raise ValueError
        origin = request.headers.get("origin")
        if require_origin and not origin:
            raise ValueError
        if origin is not None:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or _authority(parsed.netloc) != expected
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError
    except ValueError:
        raise AuthFailure("request_origin_rejected", 400) from None


def _authority(netloc: str) -> tuple[str, int]:
    parsed = urlsplit(f"https://{netloc}")
    if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError
    return parsed.hostname.lower(), parsed.port or 443


def _validate_content_type(request: Request) -> None:
    headers = request.headers
    has_body = headers.get("content-length", "0") != "0" or "transfer-encoding" in headers
    content_type = headers.get("content-type")
    if not has_body and content_type is None:
        return
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    allowed = {"application/json", "application/x-www-form-urlencoded", "multipart/form-data",
               "text/plain", "application/octet-stream"}
    if media_type not in allowed and not media_type.endswith("+json"):
        raise AuthFailure("content_type_rejected", 415)
