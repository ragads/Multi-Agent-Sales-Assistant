"""Access control helpers: signed conversation links, admin token, rate limiting."""
from __future__ import annotations
import hashlib, hmac, time, uuid
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from app.config import settings

# Link purposes. A token opens the one thing it was issued for, because the two audiences are
# not the same: a BOOKING link goes to the visitor in their calendar invite, a TRACE link goes
# to a sales rep in the lead email. Signing both with the bare session id made them the same
# string, so any visitor who booked a call could swap /booking/ for /session/ in their own
# invite URL and read the internal trace for their conversation - routing decisions, guardrail
# verdicts, retrieved chunks and their own lead score. See DECISIONS.md.
BOOKING = "booking"
TRACE = "trace"


def _key() -> bytes:
    """APP_SECRET if set; otherwise derived from the DB password so links stay stable per deployment."""
    if settings.APP_SECRET:
        return settings.APP_SECRET.encode()
    return hashlib.sha256(("cf-links:" + (urlsplit(settings.SUPABASE_DB_URL).password or "")).encode()).digest()


def sign_session(session_id: str, purpose: str = TRACE) -> str:
    """Token for ONE purpose on one session. A booking token does not open the trace page."""
    return hmac.new(_key(), f"{purpose}:{session_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _legacy_sign(session_id: str) -> str:
    """The pre-split signature: HMAC over the bare session id.

    Kept only so reschedule links in calendar invites that were already delivered keep working.
    It is accepted for BOOKING and never for TRACE, which is the leak the split closed. Delete
    this function and its one caller in require_session_access once those invites are in the past.
    """
    return hmac.new(_key(), session_id.encode(), hashlib.sha256).hexdigest()[:32]


def manage_link(session_id: str) -> str:
    """Signed self-service link for the calendar invite (reschedule / cancel)."""
    return (f"{settings.APP_BASE_URL.rstrip('/')}/booking/{session_id}"
            f"?t={sign_session(session_id, BOOKING)}")


def trace_link(session_id: str) -> str:
    """Signed link to the transcript and trace, for the lead email to the sales rep (FR-6.3)."""
    return (f"{settings.APP_BASE_URL.rstrip('/')}/session/{session_id}"
            f"?t={sign_session(session_id, TRACE)}")


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
                           x_admin_token: str | None = None, *, purpose: str = TRACE) -> None:
    """Allow the admin token, or a signed link ISSUED FOR THIS PURPOSE; anything else 404s."""
    if not valid_session_id(session_id):
        raise HTTPException(404, "no such session")
    if _admin_ok(x_admin_token):
        return
    if t and hmac.compare_digest(t, sign_session(session_id, purpose)):
        return
    # Transitional: invites already sitting in visitors' calendars carry the old
    # undifferentiated token. Honoured for the booking page only. See _legacy_sign.
    if t and purpose == BOOKING and hmac.compare_digest(t, _legacy_sign(session_id)):
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
