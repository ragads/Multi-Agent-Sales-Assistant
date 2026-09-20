"""Google Calendar MCP server (FR-5.1). Run: python -m app.mcp_servers.calendar_server

Exposes the calendar as REAL MCP tools. The Scheduler agent never calls Google directly.
Set SIMULATE_CALENDAR_OUTAGE=1 to force transient 503s (used by scripts/simulate_calendar_outage.py).
"""
from __future__ import annotations
import base64, hashlib, json, os, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings

mcp = FastMCP("closefuture-calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
_service = None


def service():
    global _service
    if _service is None:
        if settings.GOOGLE_OAUTH_REFRESH_TOKEN:
            # acts as the calendar owner: Meet links and visitor invites work on a personal Gmail
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                token=None, refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
                client_id=settings.GOOGLE_OAUTH_CLIENT_ID, client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
                token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
        else:
            info = json.loads(base64.b64decode(settings.GOOGLE_SERVICE_ACCOUNT_JSON))
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            subject = settings.GOOGLE_IMPERSONATE_USER or os.getenv("GOOGLE_IMPERSONATE_USER")
            if subject:
                creds = creds.with_subject(subject)
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _auth_mode() -> str:
    if settings.GOOGLE_OAUTH_REFRESH_TOKEN:
        return "oauth_owner"
    if settings.GOOGLE_IMPERSONATE_USER or os.getenv("GOOGLE_IMPERSONATE_USER"):
        return "service_account_delegated"
    return "service_account_plain"


# Google refuses a plain service account both Meet conferences ("Invalid conference type value", 400)
# and attendee invites ("forbiddenForServiceAccounts", 403) on a personal Gmail calendar. Reads still
# work, so the failure only shows up at booking time - name it explicitly instead of surfacing a bare
# 4xx that looks like a transient outage. See README section 2.2 Option C.
_AUTH_MODE_HINT = (
    "The calendar is authenticated as a plain service account, which Google does not allow to attach "
    "Google Meet links or invite attendees (FR-5.6, FR-5.7). Run scripts/google_oauth_setup.py to "
    "authorise as the calendar owner, or set GOOGLE_IMPERSONATE_USER for Workspace delegation."
)


def _classify_insert_error(exc: HttpError) -> dict | None:
    """Return a structured error for the two auth-mode failures, or None to fall through."""
    if _auth_mode() != "service_account_plain":
        return None
    detail = str(exc)
    status = exc.resp.status
    if status == 403 and "forbiddenForServiceAccounts" in detail:
        return {"status": "error", "error_code": "CALENDAR_AUTH_MODE", "retryable": False,
                "message": f"Cannot invite the visitor. {_AUTH_MODE_HINT}", "agent": "calendar_mcp"}
    if status == 400 and "conference" in detail.lower():
        return {"status": "error", "error_code": "CALENDAR_AUTH_MODE", "retryable": False,
                "message": f"Cannot attach a Meet link. {_AUTH_MODE_HINT}", "agent": "calendar_mcp"}
    return None


def _fail_if_simulating() -> dict | None:
    """Return a structured tool error while a simulation flag is set, else None.

    These must be structured like any other tool error, not a bare exception: classify() cannot read
    a status code out of a RuntimeError, so it falls back to retryable=True and the simulated auth
    failure gets retried three times - the opposite of what FR-8.2 says must happen.
    """
    if os.getenv("SIMULATE_CALENDAR_OUTAGE") == "1":
        return {"status": "error", "error_code": "CALENDAR_503", "retryable": True,
                "message": "calendar backend unavailable (simulated)", "agent": "calendar_mcp"}
    if os.getenv("SIMULATE_CALENDAR_AUTH_FAILURE") == "1":
        return {"status": "error", "error_code": "INVALID_CREDENTIALS", "retryable": False,
                "message": "invalid calendar credentials (simulated)", "agent": "calendar_mcp"}
    return None


# Google signals throttling with 403 plus a reason, not 429. Treating every 403 as permanent means a
# burst of bookings falls back to manual follow-up when retrying a second later would have worked.
_RETRYABLE_403_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError")


def _is_retryable(exc: HttpError) -> bool:
    status = exc.resp.status
    if status in (408, 429, 500, 502, 503, 504):
        return True
    if status == 403:
        return any(reason in str(exc) for reason in _RETRYABLE_403_REASONS)
    return False


def _http_err(exc: HttpError) -> dict:
    """Structured tool error (FR-8.3): transient failures are retryable, permanent ones are not (FR-8.2)."""
    return {"status": "error", "error_code": f"CALENDAR_{exc.resp.status}",
            "retryable": _is_retryable(exc),
            "message": str(exc)[:300], "agent": "calendar_mcp"}


def _busy(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    body = {
        "timeMin": start.isoformat(), "timeMax": end.isoformat(),
        "timeZone": settings.CALENDAR_OWNER_TZ,
        "items": [{"id": settings.GOOGLE_CALENDAR_ID}],
    }
    resp = service().freebusy().query(body=body).execute()
    blocks = resp["calendars"][settings.GOOGLE_CALENDAR_ID].get("busy", [])
    return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
             datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in blocks]


@mcp.tool()
def calendar_health() -> dict:
    """Report the credentials this RUNNING server holds, so a stale process can be spotted.

    The server caches its Google client at first use, so editing .env does nothing until it is
    restarted. Without this, an in-process check can pass while the live server still books
    without Meet links.
    """
    mode = _auth_mode()
    # `simulating` reports the flags set on THIS process. The acceptance test for tool failure keys
    # off it: the flag only has an effect here, so testing the test runner's own environment
    # skipped the test even when the server had been restarted correctly.
    simulating = None
    if os.getenv("SIMULATE_CALENDAR_OUTAGE") == "1":
        simulating = "outage"
    elif os.getenv("SIMULATE_CALENDAR_AUTH_FAILURE") == "1":
        simulating = "auth_failure"
    return {"status": "ok", "auth_mode": mode, "calendar_id": settings.GOOGLE_CALENDAR_ID,
            "meet_capable": mode != "service_account_plain",
            "invite_capable": mode != "service_account_plain",
            "simulating": simulating}


@mcp.tool()
def check_availability(start_iso: str, end_iso: str) -> dict:
    """Return busy blocks on the CloseFuture calendar between two ISO-8601 timestamps."""
    if (simulated := _fail_if_simulating()):
        return simulated
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    try:
        busy = _busy(start, end)
    except HttpError as exc:
        return _http_err(exc)
    return {"status": "ok", "busy": [{"start": b.isoformat(), "end": e.isoformat()} for b, e in busy]}


@mcp.tool()
def propose_slots(visitor_tz: str, days_ahead: int = 5, count: int = 3) -> dict:
    """Propose 2-3 concrete free slots inside business hours, rendered in the visitor's time zone.

    visitor_tz: IANA zone, e.g. 'Asia/Dubai'. Never ask the visitor to pick blindly (FR-5.3).
    """
    if (simulated := _fail_if_simulating()):
        return simulated
    owner_tz = ZoneInfo(settings.CALENDAR_OWNER_TZ)
    try:
        vtz = ZoneInfo(visitor_tz)
    except Exception:
        vtz = owner_tz

    now = datetime.now(owner_tz) + timedelta(hours=2)
    window_end = now + timedelta(days=days_ahead)
    try:
        busy = _busy(now, window_end)
    except HttpError as exc:
        return _http_err(exc)

    slots, cursor = [], now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    step = timedelta(minutes=settings.SLOT_MINUTES)
    while cursor < window_end and len(slots) < count:
        end = cursor + step
        in_hours = settings.business_start <= cursor.hour < settings.business_end
        weekday = cursor.weekday() < 5
        overlaps = any(cursor < b_end and end > b_start for b_start, b_end in busy)
        if in_hours and weekday and not overlaps:
            slots.append({
                "start_iso": cursor.isoformat(),
                "end_iso": end.isoformat(),
                "owner_label": cursor.strftime("%a %d %b, %I:%M %p ") + settings.CALENDAR_OWNER_TZ,
                "visitor_label": cursor.astimezone(vtz).strftime("%a %d %b, %I:%M %p ") + visitor_tz,
            })
            cursor += timedelta(hours=3)
        else:
            cursor += step
    return {"status": "ok", "slots": slots, "visitor_tz": visitor_tz,
            "owner_tz": settings.CALENDAR_OWNER_TZ}


_BOOK_LOCK = threading.Lock()


def _description(notes: str, manage_url: str) -> str:
    """Invite body. The manage link is what lets the visitor move or cancel without writing an email."""
    body = notes or "Discovery call booked via the CloseFuture website assistant."
    if manage_url:
        body += ("\n\nNeed a different time, or can no longer make it?\n"
                 f"Reschedule or cancel here: {manage_url}\n"
                 "The link is personal to this booking - please don't forward it.")
    return body


@mcp.tool()
def create_event(start_iso: str, end_iso: str, visitor_email: str, visitor_name: str,
                 notes: str = "", idempotency_key: str = "", manage_url: str = "") -> dict:
    """Book the discovery call: re-checks availability first, adds a Meet link, invites the visitor.

    Returns error_code SLOT_TAKEN (non-retryable) if the slot went busy in the interim (FR-5.5).
    """
    # check-then-insert must be atomic, or two simultaneous visitors can both pass the check (FR-5.5)
    with _BOOK_LOCK:
        return _create_event(start_iso, end_iso, visitor_email, visitor_name, notes, idempotency_key)


def _create_event(start_iso: str, end_iso: str, visitor_email: str, visitor_name: str,
                  notes: str = "", idempotency_key: str = "") -> dict:
    if (simulated := _fail_if_simulating()):
        return simulated
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)

    # FR-5.5: re-check immediately before insert, not only at conversation start.
    try:
        busy = _busy(start - timedelta(minutes=1), end + timedelta(minutes=1))
    except HttpError as exc:
        return _http_err(exc)
    for b_start, b_end in busy:
        if start < b_end and end > b_start:
            return {"status": "error", "error_code": "SLOT_TAKEN", "retryable": False,
                    "message": "That slot was just taken.", "agent": "calendar_mcp"}

    key = idempotency_key or hashlib.sha256((visitor_email + start_iso).encode()).hexdigest()[:24]

    # idempotency: an event with the same key already exists -> return it instead of duplicating
    try:
        existing = service().events().list(
            calendarId=settings.GOOGLE_CALENDAR_ID, privateExtendedProperty=f"idem={key}",
            timeMin=(start - timedelta(days=1)).isoformat(), maxResults=1,
        ).execute().get("items", [])
    except HttpError as exc:
        return _http_err(exc)
    if existing:
        ev = existing[0]
        return {"status": "ok", "duplicate": True, "event_id": ev["id"],
                "meet_link": ev.get("hangoutLink"), "html_link": ev.get("htmlLink"),
                "start": ev["start"]["dateTime"], "end": ev["end"]["dateTime"]}

    body = {
        "summary": f"CloseFuture discovery call - {visitor_name}",
        "description": _description(notes, manage_url),
        "start": {"dateTime": start.isoformat(), "timeZone": settings.CALENDAR_OWNER_TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.CALENDAR_OWNER_TZ},
        "attendees": [{"email": visitor_email, "displayName": visitor_name}],
        "extendedProperties": {"private": {"idem": key}},
        "conferenceData": {"createRequest": {
            "requestId": key,
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }},
        "reminders": {"useDefault": True},
    }
    try:
        ev = service().events().insert(
            calendarId=settings.GOOGLE_CALENDAR_ID, body=body,
            conferenceDataVersion=1, sendUpdates="all",  # FR-5.6 + FR-5.7
        ).execute()
    except HttpError as exc:
        misconfigured = _classify_insert_error(exc)
        if misconfigured:
            return misconfigured
        return _http_err(exc)

    # Meet provisioning is asynchronous: the insert response can come back with conferenceData in
    # status "pending" and no hangoutLink yet. Re-read once so the visitor is given a real link
    # rather than a booking that silently has none (FR-5.6).
    if not ev.get("hangoutLink") and ev.get("conferenceData"):
        pending = (ev["conferenceData"].get("createRequest", {})
                   .get("status", {}).get("statusCode") == "pending")
        if pending:
            try:
                ev = service().events().get(
                    calendarId=settings.GOOGLE_CALENDAR_ID, eventId=ev["id"]).execute()
            except HttpError:
                pass  # keep the booking; the link is also in the invite Google mails the attendee

    return {"status": "ok", "event_id": ev["id"], "meet_link": ev.get("hangoutLink"),
            "html_link": ev.get("htmlLink"), "start": ev["start"]["dateTime"],
            "end": ev["end"]["dateTime"], "attendee": visitor_email}


@mcp.tool()
def modify_event(event_id: str, new_start_iso: str, new_end_iso: str) -> dict:
    """Move an existing booking to a new time (FR-5.8). Never creates a second event."""
    if (simulated := _fail_if_simulating()):
        return simulated
    try:
        ev = service().events().patch(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="all",
            body={"start": {"dateTime": new_start_iso, "timeZone": settings.CALENDAR_OWNER_TZ},
                  "end": {"dateTime": new_end_iso, "timeZone": settings.CALENDAR_OWNER_TZ}},
        ).execute()
    except HttpError as exc:
        return _http_err(exc)
    return {"status": "ok", "event_id": ev["id"], "start": ev["start"]["dateTime"],
            "meet_link": ev.get("hangoutLink")}


@mcp.tool()
def cancel_event(event_id: str) -> dict:
    """Cancel an existing booking and notify the visitor (FR-5.8)."""
    if (simulated := _fail_if_simulating()):
        return simulated
    try:
        service().events().delete(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="all"
        ).execute()
    except HttpError as exc:
        return _http_err(exc)
    return {"status": "ok", "cancelled": event_id}


if __name__ == "__main__":
    if _auth_mode() == "service_account_plain":
        # Reads would still work, so without this the misconfiguration stays invisible until the
        # first visitor tries to book.
        print(f"[calendar] WARNING: {_AUTH_MODE_HINT}", flush=True)
    else:
        print(f"[calendar] auth mode: {_auth_mode()} on {settings.GOOGLE_CALENDAR_ID}", flush=True)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8931, path="/mcp")
