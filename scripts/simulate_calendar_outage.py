"""FR-8.1 / FR-8.2 demo: retryable outage vs non-retryable auth failure.

Usage:
  1) Restart the calendar MCP server with SIMULATE_CALENDAR_OUTAGE=1  -> 3 retries, then fallback
  2) Restart it with SIMULATE_CALENDAR_AUTH_FAILURE=1                -> classified non-retryable, 0 retries
  Then: python scripts/simulate_calendar_outage.py
"""
import asyncio

from app.mcp_client import hub
from app.observability.logger import new_trace_id
from app.reliability.retry import ToolFailure
from app.state.store import store


async def main():
    await store.connect()
    await hub.load_schemas()
    trace = new_trace_id()
    print(f"trace_id = {trace}  (query the logs table with this)\n")
    try:
        res = await hub.call("propose_slots", {"visitor_tz": "Asia/Kolkata"},
                             trace_id=trace, agent="outage_demo")
        print("tool succeeded:", res)
    except ToolFailure as tf:
        e = tf.error
        print(f"error_code={e.error_code}  retryable={e.retryable}  agent={e.agent}")
        print("message:", e.message[:200])
        print("\nCheck: retryable=True should show 3 attempts in `logs`; retryable=False exactly 1.")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
