"""Isolated, read-only FastAPI route policy inventory prototype."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import Mount, Route, WebSocketRoute


class PolicyKind(StrEnum):
    PUBLIC = "public"
    USER = "user"
    ADMIN = "admin"
    SERVICE = "service"


class PolicyConflict(ValueError):
    pass


@dataclass(frozen=True)
class AccessPolicy:
    kind: PolicyKind
    permission: str | None = None
    scope: str | None = None
    audience: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PolicyKind):
            raise ValueError("policy kind must be explicit")
        permission_policy = self.kind in {PolicyKind.USER, PolicyKind.ADMIN}
        if permission_policy != (self.permission is not None):
            raise ValueError("user/admin policy requires exactly one permission")
        if (self.kind == PolicyKind.SERVICE) != (self.scope is not None and self.audience is not None):
            raise ValueError("service policy requires exactly one scope and audience")
        if self.kind != PolicyKind.SERVICE and (self.scope is not None or self.audience is not None):
            raise ValueError("scope and audience are only valid for service policy")
        for value in (self.permission, self.scope, self.audience):
            if value is not None and (not isinstance(value, str) or not value.strip()
                                      or "*" in value or any(char.isspace() for char in value)):
                raise ValueError("policy values must be explicit non-wildcard tokens")

    @classmethod
    def public(cls) -> AccessPolicy:
        return cls(PolicyKind.PUBLIC)

    @classmethod
    def user(cls, permission: str) -> AccessPolicy:
        return cls(PolicyKind.USER, permission=permission)

    @classmethod
    def admin(cls, permission: str) -> AccessPolicy:
        return cls(PolicyKind.ADMIN, permission=permission)

    @classmethod
    def service(cls, scope: str, audience: str) -> AccessPolicy:
        return cls(PolicyKind.SERVICE, scope=scope, audience=audience)


def declare_policy(target: Any, method: str, policy: AccessPolicy) -> None:
    """Attach explicit per-method metadata to an endpoint or mounted route."""
    method = method.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", method):
        raise ValueError(f"unsupported inventory method: {method}")
    if isinstance(target, (APIRoute, Route, WebSocketRoute)):
        target = target.endpoint
    policies = dict(getattr(target, "__http_access_policies__", {}))
    if method in policies:
        raise PolicyConflict(f"policy already declared for {method}")
    policies[method] = policy
    setattr(target, "__http_access_policies__", policies)


@dataclass
class InventorySnapshot:
    fingerprint: str
    entries: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    stale: bool = False

    @property
    def valid(self) -> bool:
        return not self.stale and not any(item["severity"] == "error" for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "valid": self.valid, "stale": self.stale,
                "entries": list(self.entries), "findings": list(self.findings)}


class UnclassifiedRoutes(RuntimeError):
    def __init__(self, snapshot: InventorySnapshot):
        super().__init__(f"HTTP policy inventory has {sum(f['severity'] == 'error' for f in snapshot.findings)} error(s)")
        self.snapshot = snapshot


def inspect_routes(app: Any, *, strict: bool = False) -> InventorySnapshot:
    """Describe registered routes in order; strict mode rejects every error."""
    entries: list[dict[str, Any]] = []
    route_records: list[tuple[int, str, tuple[str, ...], Any, str]] = []

    def walk(routes: list[Any], prefix: str = "") -> None:
        for route in routes:
            path = _join(prefix, getattr(route, "path", ""))
            if isinstance(route, APIRoute):
                kind, methods, endpoint = "http", tuple(sorted(route.methods or ())), route.endpoint
            elif isinstance(route, WebSocketRoute):
                kind, methods, endpoint = "websocket", ("WEBSOCKET",), route.endpoint
            elif isinstance(route, Mount):
                kind, methods, endpoint = "mount", ("MOUNT",), route
            elif isinstance(route, Route):
                kind, methods, endpoint = "http", tuple(sorted(route.methods or ())), route.endpoint
            else:
                kind, methods, endpoint = "framework", ("UNKNOWN",), route

            declared = getattr(endpoint, "__http_access_policies__", {})
            method_policies = {method: _policy_dict(declared[method]) for method in methods if method in declared}
            module = getattr(getattr(route, "endpoint", None), "__module__", "")
            name = getattr(route, "name", type(route).__name__)
            source = _source(module, name, kind, route)
            index = len(entries)
            entries.append({"order": index, "kind": kind, "path": path, "methods": list(methods),
                            "name": name, "source": source, "policies": method_policies})
            route_records.append((index, path, methods, route, kind))

            if isinstance(route, Mount):
                nested = getattr(route.app, "routes", None)
                if nested is None:
                    nested = getattr(getattr(route.app, "router", None), "routes", None)
                if nested is not None:
                    walk(list(nested), path)

    walk(list(app.routes))
    findings: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], int] = {}

    for entry, (_, path, methods, route, _kind) in zip(entries, route_records):
        declared = getattr(route.endpoint if hasattr(route, "endpoint") else route,
                           "__http_access_policies__", {})
        for method in methods:
            if method not in declared:
                findings.append(_finding("missing_policy", "error", entry["order"], method, path))
        for method in set(declared) - set(methods):
            findings.append(_finding("policy_method_mismatch", "error", entry["order"], method, path))

        for method in methods:
            key = (_normalized_path(path), method, entry["kind"])
            if key in seen:
                findings.append({"code": "duplicate_route", "severity": "error", "order": entry["order"],
                                 "conflicts_with": seen[key], "method": method, "path": path})
            else:
                seen[key] = entry["order"]

    catchall_overlaps: dict[int, list[int]] = {}
    for later_index, later_path, later_methods, later_route, later_kind in route_records:
        if later_kind not in {"http", "websocket"}:
            continue
        for earlier_index, earlier_path, earlier_methods, earlier_route, earlier_kind in route_records[:later_index]:
            if earlier_kind not in {"http", "websocket"}:
                continue
            common = sorted(set(earlier_methods) & set(later_methods))
            if common and _paths_may_overlap(earlier_path, later_path):
                if _is_catchall(later_path):
                    catchall_overlaps.setdefault(later_index, []).append(earlier_index)
                else:
                    findings.append({"code": "matching_overlap",
                                     "severity": "error" if _specificity(earlier_path) < _specificity(later_path) else "warning",
                                     "order": later_index, "conflicts_with": earlier_index, "methods": common,
                                     "earlier_path": earlier_path, "later_path": later_path,
                                     "registration_order": "earlier route matches first"})

    for order, conflicts in catchall_overlaps.items():
        findings.append({"code": "catchall_fallback_overlap", "severity": "warning", "order": order,
                         "conflicts_with": conflicts, "path": entries[order]["path"],
                         "detail": "later fallback preserves earlier matches but may capture unknown paths"})

    findings.sort(key=lambda item: (item["order"], item["code"], item.get("method", ""), item.get("conflicts_with", -1)))
    canonical = json.dumps({"entries": entries, "findings": findings}, sort_keys=True, separators=(",", ":"))
    snapshot = InventorySnapshot(hashlib.sha256(canonical.encode()).hexdigest(), tuple(entries), tuple(findings))
    if strict and not snapshot.valid:
        raise UnclassifiedRoutes(snapshot)
    return snapshot


def assert_snapshot_current(app: Any, snapshot: InventorySnapshot) -> None:
    """Raise if any route, method, source or policy changed after snapshot."""
    current = inspect_routes(app)
    if current.fingerprint != snapshot.fingerprint:
        snapshot.stale = True
        raise RuntimeError("route policy inventory snapshot is stale")


def _policy_dict(policy: AccessPolicy) -> dict[str, str]:
    return {key: value for key, value in asdict(policy).items() if value is not None} | {"kind": policy.kind.value}


def _source(module: str, name: str, kind: str, route: Any) -> str:
    if module.startswith(("fastapi.", "starlette.")) or name in {"openapi", "swagger_ui_html", "redoc_html", "oauth2_redirect"}:
        return "framework"
    if kind == "mount" and type(getattr(route, "app", None)).__name__ == "StaticFiles":
        return "framework:static-files"
    if module.startswith("app.modules."):
        return "module:" + module.removeprefix("app.modules.").split(".", 1)[0]
    return "application" if module else "unknown"


def _finding(code: str, severity: str, order: int, method: str, path: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "order": order, "method": method, "path": path}


def _join(prefix: str, path: str) -> str:
    return "/" + "/".join(part.strip("/") for part in (prefix, path) if part.strip("/"))


def _normalized_path(path: str) -> str:
    return re.sub(r"\{[^}:]+(?::([^}]+))?\}", lambda match: "{" + (match.group(1) or "str") + "}", path)


def _path_parts(path: str) -> list[tuple[str, str | None]]:
    parts = []
    for part in path.strip("/").split("/") if path.strip("/") else []:
        match = re.fullmatch(r"\{[^}:]+(?::([^}]+))?\}", part)
        parts.append(("", match.group(1) or "str") if match else (part, None))
    return parts


def _paths_may_overlap(left: str, right: str) -> bool:
    a, b = _path_parts(left), _path_parts(right)
    path_a = bool(a and a[-1][1] == "path")
    path_b = bool(b and b[-1][1] == "path")
    if path_a:
        a = a[:-1]
    if path_b:
        b = b[:-1]
    if path_a and path_b:
        if len(a) != len(b):
            return False
    elif path_a and a and len(b) <= len(a):
        return False
    elif path_b and b and len(a) <= len(b):
        return False
    if not (path_a or path_b) and len(a) != len(b):
        return False
    common = min(len(a), len(b))
    return all(not (a[index][0] and b[index][0] and a[index][0] != b[index][0])
               for index in range(common))


def _is_catchall(path: str) -> bool:
    parts = _path_parts(path)
    return bool(parts and parts[-1][1] == "path" and not any(value for value, _ in parts))


def _specificity(path: str) -> tuple[int, int]:
    parts = _path_parts(path)
    literals = sum(bool(value) for value, _ in parts)
    parameters = sum(converter is not None for _, converter in parts)
    return literals, -parameters
