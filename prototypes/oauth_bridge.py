"""In-memory pairing state machine; transport and NEXO auth are intentionally absent."""

from dataclasses import dataclass
import hashlib
import secrets
from urllib.parse import urlsplit


class PairingError(ValueError):
    pass


@dataclass
class Pairing:
    principal: str
    expires_at: float
    code_digest: bytes
    payload: dict | None = None


class PairingBridge:
    def __init__(self, allowed_origin: str, ttl_seconds: int = 300) -> None:
        parsed = urlsplit(allowed_origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("allowed_origin must be an HTTPS origin")
        self.allowed_origin = f"https://{parsed.netloc.lower()}"
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Pairing] = {}

    def begin(self, principal: str, now: float) -> tuple[str, str]:
        if not principal:
            raise PairingError("authorized principal required")
        session_id = secrets.token_urlsafe(24)
        code = secrets.token_urlsafe(32)
        self._sessions[session_id] = Pairing(
            principal, now + self.ttl_seconds, hashlib.sha256(code.encode()).digest()
        )
        return session_id, code

    def transfer(self, session_id: str, code: str, destination: str, credentials: dict, now: float) -> None:
        parsed = urlsplit(destination)
        origin = f"{parsed.scheme}://{parsed.netloc.lower()}" if parsed.netloc else ""
        if origin != self.allowed_origin or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PairingError("destination rejected")
        pairing = self._sessions.get(session_id)
        if pairing is None or now >= pairing.expires_at:
            self._sessions.pop(session_id, None)
            raise PairingError("pairing unavailable")
        if not secrets.compare_digest(hashlib.sha256(code.encode()).digest(), pairing.code_digest):
            raise PairingError("pairing unavailable")
        if pairing.payload is not None:
            raise PairingError("pairing unavailable")
        pairing.payload = dict(credentials)

    def confirm(self, session_id: str, principal: str, now: float) -> dict:
        pairing = self._sessions.get(session_id)
        if pairing is None or now >= pairing.expires_at or pairing.principal != principal or pairing.payload is None:
            self._sessions.pop(session_id, None) if pairing is not None and now >= pairing.expires_at else None
            raise PairingError("pairing unavailable")
        credentials = pairing.payload
        del self._sessions[session_id]
        return credentials

    def diagnostics(self) -> dict[str, int]:
        return {"pending_pairings": len(self._sessions)}
