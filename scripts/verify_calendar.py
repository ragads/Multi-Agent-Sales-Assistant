"""Prove the calendar integration end to end: real event, real Meet link, real visitor invite.

This is the FR-5.6 / FR-5.7 check. It drives the same MCP tool functions the Scheduler agent calls,
so a pass here means the agent path works - not just that credentials exist.

    python scripts/verify_calendar.py                     # invite the calendar owner
    python scripts/verify_calendar.py visitor@example.com # invite a real visitor address
    python scripts/verify_calendar.py --keep              # leave the test event on the calendar

The test event is booked ~200 days out at 03:00 so it cannot collide with a real meeting, and is
cancelled again unless --keep is passed.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings                      # noqa: E402
from app.mcp_servers import calendar_server as cal   # noqa: E402

args = [a for a in sys.argv[1:] if a != "--keep"]
keep = "--keep" in sys.argv
visitor_email = args[0] if args else settings.GOOGLE_CALENDAR_ID

ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


mode = "OAuth (calendar owner)" if settings.GOOGLE_OAUTH_REFRESH_TOKEN else (
    f"service account impersonating {settings.GOOGLE_IMPERSONATE_USER}"
    if settings.GOOGLE_IMPERSONATE_USER else "service account (no delegation)")
print(f"Calendar : {settings.GOOGLE_CALENDAR_ID}")
print(f"Auth mode: {mode}")
print(f"Invitee  : {visitor_email}\n")

if not settings.GOOGLE_OAUTH_REFRESH_TOKEN and not settings.GOOGLE_IMPERSONATE_USER:
    print("  NOTE: in this mode Google refuses Meet links and attendee invites on a personal Gmail\n"
          "        calendar. Run scripts/google_oauth_setup.py first.\n")

print("0. Live MCP server on " + settings.MCP_CALENDAR_URL)
# Steps 1-5 below call the tool functions in-process, so they read .env fresh and pass even when the
# running server still holds older credentials. That mismatch is invisible until a real visitor books,
# so check the live server's own view first.
try:
    import asyncio
    from fastmcp import Client

    async def _health() -> dict:
        async with Client(settings.MCP_CALENDAR_URL) as c:
            res = await c.call_tool("calendar_health", {})
            blocks = res if isinstance(res, list) else getattr(res, "content", None) or []
            import json as _json
            return _json.loads(getattr(blocks[0], "text", "{}")) if blocks else {}

    live = asyncio.run(_health())
    live_mode = live.get("auth_mode", "?")
    check("running server reachable", bool(live), settings.MCP_CALENDAR_URL)
    check("running server uses the same credentials as .env", live_mode == (
        "oauth_owner" if settings.GOOGLE_OAUTH_REFRESH_TOKEN else
        "service_account_delegated" if settings.GOOGLE_IMPERSONATE_USER else "service_account_plain"),
        f"server={live_mode}" + ("  <- RESTART THE CALENDAR MCP SERVER" if live_mode == "service_account_plain"
                                 and settings.GOOGLE_OAUTH_REFRESH_TOKEN else ""))
except Exception as exc:  # server not running is fine - the in-process checks still mean something
    print(f"  [SKIP] server not reachable ({str(exc)[:80]}) - checking in-process only")

print("\n1. Reading availability (FR-5.2)")
tz = ZoneInfo(settings.CALENDAR_OWNER_TZ)
now = datetime.now(tz)
avail = cal.check_availability(now.isoformat(), (now + timedelta(days=7)).isoformat())
check("check_availability returns busy blocks", avail.get("status") == "ok", str(avail)[:160])

print("\n2. Proposing slots in the visitor's time zone (FR-5.3, FR-5.4)")
proposed = cal.propose_slots("Asia/Dubai")
slots = proposed.get("slots", [])
check("propose_slots returns 2-3 concrete slots", 2 <= len(slots) <= 3, f"{len(slots)} returned")
check("slots are labelled in both time zones",
      bool(slots) and "visitor_label" in slots[0] and "owner_label" in slots[0],
      slots[0]["visitor_label"] if slots else "")

print("\n3. Booking a real event (FR-5.5, FR-5.6, FR-5.7)")
start = (now + timedelta(days=200)).replace(hour=3, minute=0, second=0, microsecond=0)
end = start + timedelta(minutes=settings.SLOT_MINUTES)
ev = cal.create_event(start.isoformat(), end.isoformat(), visitor_email, "Verification Visitor",
                      notes="Automated check from scripts/verify_calendar.py",
                      idempotency_key=f"verify-{start.date()}")

if ev.get("status") == "error":
    check("create_event succeeded", False, f"{ev.get('error_code')}: {ev.get('message', '')[:200]}")
else:
    check("create_event returned an event id", bool(ev.get("event_id")), ev.get("event_id", ""))
    check("Google Meet link attached (FR-5.6)", bool(ev.get("meet_link")), ev.get("meet_link") or "none")
    check("visitor invited (FR-5.7)", ev.get("attendee") == visitor_email, ev.get("attendee") or "none")

    print("\n4. Re-check blocks a double booking (FR-5.5)")
    dupe = cal.create_event(start.isoformat(), end.isoformat(), visitor_email, "Second Visitor",
                            idempotency_key="verify-different-key")
    check("second booking for the same slot refused",
          dupe.get("error_code") == "SLOT_TAKEN" or dupe.get("duplicate") is True,
          dupe.get("error_code") or ("duplicate" if dupe.get("duplicate") else str(dupe)[:120]))

    print("\n5. Rescheduling it in place (FR-5.8)")
    moved_start = start + timedelta(hours=2)
    moved_end = moved_start + timedelta(minutes=settings.SLOT_MINUTES)
    moved = cal.modify_event(ev["event_id"], moved_start.isoformat(), moved_end.isoformat())
    check("modify_event moved the booking", moved.get("status") == "ok", str(moved)[:160])
    # the point of FR-5.8 is that rescheduling edits the booking rather than leaving a second one
    check("same event id - no duplicate created", moved.get("event_id") == ev["event_id"],
          f"{moved.get('event_id')} vs {ev['event_id']}")

    if keep:
        print(f"\n--keep: leaving event {ev['event_id']} on the calendar.")
    else:
        print("\n6. Cancelling the test event (FR-5.8)")
        cancelled = cal.cancel_event(ev["event_id"])
        check("cancel_event removed it", cancelled.get("status") == "ok", str(cancelled)[:160])

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED - see [FAIL] lines above"))
sys.exit(0 if ok else 1)
