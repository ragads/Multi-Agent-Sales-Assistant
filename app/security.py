"""Access control helpers: signed conversation links, admin token, rate limiting."""
from __future__ import annotations
import hashlib, hmac, time, uuid
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from app.config import settings


def _key() -> bytes:
    """APP_SECRET if set; otherwise derived from the DB password so links stay stable per deployment."""
    if settings.APP_SECRET:
        return settings.APP_SECRET.encode()
    return hashlib.sha256(("cf-links:" + (urlsplit(settings.SUPABASE_DB_URL).password or "")).encode()).digest()


def sign_session(session_id: str) -> str:
    """Token that lets a sales rep open one conversation from the lead email (FR-6.3)."""
    return hmac.new(_key(), session_id.encode(), hashlib.sha256).hexdigest()[:32]


def valid_session_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _admin_ok(token: str | None) -> bool:
    return bool(settings.ADMIN_TOKEN) and bool(token) and hmac.compare_digest(token, settings.ADMIN_TOKEN)


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not _admin_ok(x_admin_token):
        raise HTTPException(403, "admin token required")


def require_session_access(session_id: str, t: str | None = None,
                           x_admin_token: str | None = None) -> None:
    """Allow a valid signed link or the admin token; anything else looks like a missing page."""
    if not valid_session_id(session_id):
        raise HTTPException(404, "no such session")
    if _admin_ok(x_admin_token):
        return
    if t and hmac.compare_digest(t, sign_session(session_id)):
        return
    raise HTTPException(404, "no such session")


class RateLimiter:
    """In-memory sliding window. One process only; put a shared store behind it to scale out."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
