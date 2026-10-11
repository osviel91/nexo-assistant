"""Isolated admin/session contract; not imported by the NEXO runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets


class AuthError(ValueError):
    pass


@dataclass
class Session:
    user_id: str
    expires_at: float
    csrf_digest: bytes


class AdminSecurity:
    def __init__(self, user_id: str, password: str, *, session_ttl: int = 1800,
                 max_attempts: int = 5, lock_seconds: int = 300) -> None:
        if not user_id or len(password) < 12:
            raise ValueError("initial admin requires an id and a 12-character password")
        self.user_id = user_id
        salt = secrets.token_bytes(16)
        self._password_salt = salt
        self._password_digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        self.session_ttl = session_ttl
        self.max_attempts = max_attempts
        self.lock_seconds = lock_seconds
        self._sessions: dict[bytes, Session] = {}
        self._failures: dict[str, tuple[int, float]] = {}

    def login(self, user_id: str, password: str, now: float) -> tuple[str, str]:
        attempts, locked_until = self._failures.get(user_id, (0, 0))
        if now < locked_until:
            raise AuthError("authentication unavailable")
        digest = hashlib.scrypt(password.encode(), salt=self._password_salt, n=2**14, r=8, p=1)
        if user_id != self.user_id or not hmac.compare_digest(digest, self._password_digest):
            attempts += 1
            self._failures[user_id] = (attempts, now + self.lock_seconds if attempts >= self.max_attempts else 0)
            raise AuthError("authentication unavailable")
        self._failures.pop(user_id, None)
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        self._sessions[hashlib.sha256(session_token.encode()).digest()] = Session(
            user_id, now + self.session_ttl, hashlib.sha256(csrf_token.encode()).digest()
        )
        return session_token, csrf_token

    def authorize(self, session_token: str, permission: str, *, now: float,
                  csrf_token: str | None = None, mutating: bool = False) -> str:
        digest = hashlib.sha256(session_token.encode()).digest()
        session = self._sessions.get(digest)
        if session is None or now >= session.expires_at:
            self._sessions.pop(digest, None)
            raise AuthError("authentication required")
        if permission != "admin:*":
            raise AuthError("permission denied")
        if mutating and (csrf_token is None or not hmac.compare_digest(
                hashlib.sha256(csrf_token.encode()).digest(), session.csrf_digest)):
            raise AuthError("csrf validation failed")
        return session.user_id

    def logout(self, session_token: str) -> None:
        self._sessions.pop(hashlib.sha256(session_token.encode()).digest(), None)

    @staticmethod
    def authorize_service(service_id: str, permission: str) -> str:
        # Human sessions cannot call this contract; service credentials are not implemented here.
        raise AuthError("service identity required")
