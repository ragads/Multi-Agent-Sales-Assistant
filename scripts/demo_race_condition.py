"""FR-5.5 demo: two bookings fire at the same slot concurrently -> one wins, one gets SLOT_TAKEN.

Run the MCP calendar server first, then:  python scripts/demo_race_condition.py
"""
import asyncio, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.mcp_client import hub
from app.observability.logger import new_trace_id
from app.reliability.retry import ToolFailure
from app.state.store import store


async def book(tag: str, start, end):
    try:
        res = await hub.call("create_event", {
            "start_iso": start, "end_iso": end,
            "visitor_email": f"{tag}@example.com", "visitor_name": f"Race Test {tag}",
            "notes": "race-condition demo", "idempotency_key": f"race-{tag}",
        }, trace_id=new_trace_id(), agent="race_demo")
        return tag, "BOOKED", res.get("event_id")
    except ToolFailure as tf:
        return tag, tf.error.error_code, tf.error.message


async def main():
    await store.connect()
    await hub.load_schemas()
    tz = ZoneInfo(settings.CALENDAR_OWNER_TZ)
    slot = (datetime.now(tz) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    start, end = slot.isoformat(), (slot + timedelta(minutes=settings.SLOT_MINUTES)).isoformat()
    print(f"Both visitors attempt {start}\n")

    a, b = await asyncio.gather(book("alpha", start, end), book("bravo", start, end))
    for tag, outcome, detail in (a, b):
        print(f"  {tag:6} -> {outcome:18} {str(detail)[:60]}")
    print("\nExpected: exactly one BOOKED, the other SLOT_TAKEN (non-retryable).")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
