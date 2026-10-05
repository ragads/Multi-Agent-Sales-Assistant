"""Google Calendar MCP server (FR-5.1). Run: python -m app.mcp_servers.calendar_server

Exposes the calendar as REAL MCP tools. The Scheduler agent never calls Google directly.
The server is the last line of defence for booking rules: create_event and modify_event reject any slot
that is not exactly SLOT_MINUTES long, inside business hours on a weekday, and in the future - whatever
the caller sends. Requests need `Authorization: Bearer $MCP_CALENDAR_TOKEN` (app/mcp_servers/auth.py).
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

from app.booking_rules import SlotRuleError, validate_slot, within_business_hours
from app.config import settings
from app.mcp_servers.auth import serve

mcp = FastMCP("closefuture-calendar")

# Serialises create_event and modify_event: each one checks free/busy and then writes, and two calls
# interleaving between those steps could both see a slot as free (FR-5.5).
_BOOK_LOCK = threading.Lock()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
_service = None


def service():
    global _service
    if _service is None:
        if settings.GOOGLE_OAUTH_REFRESH_TOKEN:
            # act as the calendar owner: the only way to send invites + Meet links on a Gmail calendar
            from google.oauth2.credentials import Credentials
            creds = Credentials(
                token=None, refresh_token=settings.GOOGLE_OAUTH_REFRESH_TOKEN,
                client_id=settings.GOOGLE_OAUTH_CLIENT_ID, client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
                token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
        else:
            info = json.loads(base64.b64decode(settings.GOOGLE_SERVICE_ACCOUNT_JSON))
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            subject = os.getenv("GOOGLE_IMPERSONATE_USER")
            if subject:
                creds = creds.with_subject(subject)
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _fail_if_simulating():
    if os.getenv("SIMULATE_CALENDAR_OUTAGE") == "1":
        raise RuntimeError("HTTP 503: calendar backend unavailable (simulated)")
    if os.getenv("SIMULATE_CALENDAR_AUTH_FAILURE") == "1":
        raise RuntimeError("HTTP 401: invalid credentials (simulated)")


def _check_slot(start_iso: str, end_iso: str) -> tuple[datetime, datetime]:
    return validate_slot(start_iso, end_iso, owner_tz=settings.CALENDAR_OWNER_TZ,
                         opens=settings.business_start, closes=settings.business_end,
                         slot_minutes=settings.SLOT_MINUTES)


def _rule_error(exc: SlotRuleError) -> dict:
    return {"status": "error", "error_code": exc.code, "retryable": False,
            "message": str(exc), "agent": "calendar_mcp"}


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
    busy = _busy(start, end)
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
    busy = _busy(now, window_end)

    slots, cursor = [], now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    step = timedelta(minutes=settings.SLOT_MINUTES)
    while cursor < window_end and len(slots) < count:
        end = cursor + step
        # the same rule create_event enforces, so every proposed slot is bookable
        in_hours = within_business_hours(cursor, end, settings.business_start, settings.business_end)
        overlaps = any(cursor < b_end and end > b_start for b_start, b_end in busy)
        if in_hours and not overlaps:
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

    Returns error_code SLOT_TAKEN (non-retryable) if the slot went busy in the interim (FR-5.5), and
    INVALID_DURATION / OUTSIDE_BUSINESS_HOURS / SLOT_IN_PAST / MALFORMED_TIME if the slot breaks the
    booking rules (30 minutes, weekday business hours, in the future).
    """
    # check-then-insert must be atomic, or two simultaneous visitors can both pass the check (FR-5.5)
    with _BOOK_LOCK:
        return _create_event(start_iso, end_iso, visitor_email, visitor_name, notes,
                             idempotency_key, manage_url)


def _create_event(start_iso: str, end_iso: str, visitor_email: str, visitor_name: str,
                  notes: str, idempotency_key: str, manage_url: str) -> dict:
    _fail_if_simulating()
    try:
        start, end = _check_slot(start_iso, end_iso)
    except SlotRuleError as exc:
        return _rule_error(exc)

    key = idempotency_key or hashlib.sha256((visitor_email + start_iso).encode()).hexdigest()[:24]

    # idempotency first: a retried create finds its own event and returns it - checked before the
    # availability test, which that same event would otherwise fail as "slot taken"
    existing = service().events().list(
        calendarId=settings.GOOGLE_CALENDAR_ID, privateExtendedProperty=f"idem={key}",
        timeMin=(start - timedelta(days=1)).isoformat(), maxResults=1,
    ).execute().get("items", [])
    if existing and existing[0].get("status") != "cancelled":
        ev = existing[0]
        return {"status": "ok", "duplicate": True, "event_id": ev["id"],
                "meet_link": ev.get("hangoutLink"), "html_link": ev.get("htmlLink"),
                "start": ev["start"]["dateTime"], "end": ev["end"]["dateTime"]}

    # FR-5.5: re-check immediately before insert, not only at conversation start.
    for b_start, b_end in _busy(start - timedelta(minutes=1), end + timedelta(minutes=1)):
        if start < b_end and end > b_start:
            return {"status": "error", "error_code": "SLOT_TAKEN", "retryable": False,
                    "message": "That slot was just taken.", "agent": "calendar_mcp"}

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
        retryable = exc.resp.status in (429, 500, 502, 503, 504)
        return {"status": "error", "error_code": f"CALENDAR_{exc.resp.status}",
                "retryable": retryable, "message": str(exc), "agent": "calendar_mcp"}

    return {"status": "ok", "event_id": ev["id"], "meet_link": ev.get("hangoutLink"),
            "html_link": ev.get("htmlLink"), "start": ev["start"]["dateTime"],
            "end": ev["end"]["dateTime"], "attendee": visitor_email}


@mcp.tool()
def modify_event(event_id: str, new_start_iso: str, new_end_iso: str) -> dict:
    """Move an existing booking to a new time (FR-5.8). Never creates a second event.

    Same rules as create_event: a valid 30-minute business-hours slot that is free (the booking being
    moved does not count against itself). Shares the booking lock, so a move and a new booking can't
    both claim the same free slot.
    """
    with _BOOK_LOCK:
        return _modify_event(event_id, new_start_iso, new_end_iso)


def _modify_event(event_id: str, new_start_iso: str, new_end_iso: str) -> dict:
    _fail_if_simulating()
    try:
        start, end = _check_slot(new_start_iso, new_end_iso)
    except SlotRuleError as exc:
        return _rule_error(exc)
    try:
        current = service().events().get(calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        own = (datetime.fromisoformat(current["start"]["dateTime"].replace("Z", "+00:00")),
               datetime.fromisoformat(current["end"]["dateTime"].replace("Z", "+00:00")))
        for b_start, b_end in _busy(start - timedelta(minutes=1), end + timedelta(minutes=1)):
            if (b_start, b_end) != own and start < b_end and end > b_start:
                return {"status": "error", "error_code": "SLOT_TAKEN", "retryable": False,
                        "message": "That slot is not free.", "agent": "calendar_mcp"}
        ev = service().events().patch(
            calendarId=settings.GOOGLE_CALENDAR_ID, eventId=event_id, sendUpdates="all",
            body={"start": {"dateTime": start.isoformat(), "timeZone": settings.CALENDAR_OWNER_TZ},
                  "end": {"dateTime": end.isoformat(), "timeZone": settings.CALENDAR_OWNER_TZ}},
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
    serve(mcp, port=8931, token=settings.MCP_CALENDAR_TOKEN)
