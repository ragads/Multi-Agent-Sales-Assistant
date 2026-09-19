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


def _fail_if_simulating():
    if os.getenv("SIMULATE_CALENDAR_OUTAGE") == "1":
        raise RuntimeError("HTTP 503: calendar backend unavailable (simulated)")
    if os.getenv("SIMULATE_CALENDAR_AUTH_FAILURE") == "1":
        raise RuntimeError("HTTP 401: invalid credentials (simulated)")


def _http_err(exc: HttpError) -> dict:
    """Structured tool error (FR-8.3): 5xx/429 are retryable, everything else is not (FR-8.2)."""
    status = exc.resp.status
    return {"status": "error", "error_code": f"CALENDAR_{status}",
            "retryable": status in (408, 429, 500, 502, 503, 504),
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
def check_availability(start_iso: str, end_iso: str) -> dict:
    """Return busy blocks on the CloseFuture calendar between two ISO-8601 timestamps."""
    _fail_if_simulating()
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
    _fail_if_simulating()
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


@mcp.tool()
def create_event(start_iso: str, end_iso: str, visitor_email: str, visitor_name: str,
                 notes: str = "", idempotency_key: str = "") -> dict:
    """Book the discovery call: re-checks availability first, adds a Meet link, invites the visitor.

    Returns error_code SLOT_TAKEN (non-retryable) if the slot went busy in the interim (FR-5.5).
    """
    # check-then-insert must be atomic, or two simultaneous visitors can both pass the check (FR-5.5)
    with _BOOK_LOCK:
        return _create_event(start_iso, end_iso, visitor_email, visitor_name, notes, idempotency_key)


def _create_event(start_iso: str, end_iso: str, visitor_email: str, visitor_name: str,
                  notes: str = "", idempotency_key: str = "") -> dict:
    _fail_if_simulating()
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
        "description": (notes or "Discovery call booked via the CloseFuture website assistant."),
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
        retryable = exc.resp.status in (429, 500, 502, 503, 504)
        return {"status": "error", "error_code": f"CALENDAR_{exc.resp.status}",
                "retryable": retryable, "message": str(exc), "agent": "calendar_mcp"}

    return {"status": "ok", "event_id": ev["id"], "meet_link": ev.get("hangoutLink"),
            "html_link": ev.get("htmlLink"), "start": ev["start"]["dateTime"],
            "end": ev["end"]["dateTime"], "attendee": visitor_email}


@mcp.tool()
def modify_event(event_id: str, new_start_iso: str, new_end_iso: str) -> dict:
    """Move an existing booking to a new time (FR-5.8). Never creates a second event."""
    _fail_if_simulating()
    try:
        ev = service().events().patch(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="all",
            body={"start": {"dateTime": new_start_iso, "timeZone": settings.CALENDAR_OWNER_TZ},
                  "end": {"dateTime": new_end_iso, "timeZone": settings.CALENDAR_OWNER_TZ}},
        ).execute()
    except HttpError as exc:
        return {"status": "error", "error_code": f"CALENDAR_{exc.resp.status}",
                "retryable": exc.resp.status >= 500, "message": str(exc), "agent": "calendar_mcp"}
    return {"status": "ok", "event_id": ev["id"], "start": ev["start"]["dateTime"],
            "meet_link": ev.get("hangoutLink")}


@mcp.tool()
def cancel_event(event_id: str) -> dict:
    """Cancel an existing booking and notify the visitor (FR-5.8)."""
    _fail_if_simulating()
    try:
        service().events().delete(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="all"
        ).execute()
    except HttpError as exc:
        return {"status": "error", "error_code": f"CALENDAR_{exc.resp.status}",
                "retryable": exc.resp.status >= 500, "message": str(exc), "agent": "calendar_mcp"}
    return {"status": "ok", "cancelled": event_id}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8931, path="/mcp")
