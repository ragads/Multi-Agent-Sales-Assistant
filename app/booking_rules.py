"""Booking rules, as pure functions (FR-5.2, FR-5.5).

Shared by the calendar MCP server (which enforces them on every write) and the Scheduler agent (which
checks the model's tool arguments against what the server offered). No I/O here, so the rules are
unit-tested offline.
"""
from __future__ import annotations
import re
from datetime import datetime, time, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class SlotRuleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_iso(value: str, tz: ZoneInfo) -> datetime:
    """Parse ISO-8601; a naive timestamp is read in the calendar owner's zone."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise SlotRuleError("MALFORMED_TIME", f"not an ISO-8601 timestamp: {value!r}")
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def within_business_hours(start: datetime, end: datetime, opens: time, closes: time) -> bool:
    """Weekday, same day, and the whole meeting inside opening hours (both in the owner's zone)."""
    return (start.weekday() < 5 and start.date() == end.date()
            and opens <= start.time() and end.time() <= closes)


def validate_slot(start_iso: str, end_iso: str, *, owner_tz: str, opens: time, closes: time,
                  slot_minutes: int, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Raise SlotRuleError unless this is a bookable discovery-call slot. Returns owner-zone datetimes."""
    tz = ZoneInfo(owner_tz)
    start = parse_iso(start_iso, tz).astimezone(tz)
    end = parse_iso(end_iso, tz).astimezone(tz)
    now = (now or datetime.now(tz)).astimezone(tz)

    if end - start != timedelta(minutes=slot_minutes):
        raise SlotRuleError("INVALID_DURATION",
                            f"discovery calls are exactly {slot_minutes} minutes")
    if start <= now:
        raise SlotRuleError("SLOT_IN_PAST", "that time has already passed")
    if not within_business_hours(start, end, opens, closes):
        raise SlotRuleError("OUTSIDE_BUSINESS_HOURS",
                            f"calls run Monday-Friday, {opens:%H:%M}-{closes:%H:%M} {owner_tz}")
    return start, end


def match_offered_slot(start_iso: str, offered: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The offered slot with this start instant, comparing instants rather than strings."""
    try:
        want = datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        return None
    for slot in offered:
        try:
            got = datetime.fromisoformat(slot["start_iso"])
        except (KeyError, TypeError, ValueError):
            continue
        if (want.tzinfo is None) == (got.tzinfo is None) and want == got:
            return slot
    return None


def email_given_by_visitor(email: str, visitor_messages: Iterable[str],
                           known_email: Optional[str] = None) -> bool:
    """True only if the visitor actually typed this address (or it is already on the session)."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return False
    if known_email and known_email.strip().lower() == email:
        return True
    return any(email == e.lower() for m in visitor_messages for e in EMAIL_RE.findall(m or ""))
