"""Email outbox drain: a failed send is retried, never lost (FR-8.1)."""
from __future__ import annotations

from app.mcp_client import hub
from app.observability.logger import log_event, new_trace_id
from app.reliability.retry import ToolFailure
from app.state.store import store


async def drain() -> int:
    sent = 0
    for row in await store.outbox_pending():
        trace_id = new_trace_id()
        payload = row["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        try:
            result = await hub.call("send_lead_summary", {"summary": payload},
                                    trace_id=trace_id, session_id=str(row["session_id"]),
                                    agent="outbox")
            await store.outbox_mark(str(row["id"]), "sent", provider_id=result.get("provider_id"))
            sent += 1
        except ToolFailure as tf:
            status = "pending" if tf.error.retryable and row["attempts"] < 11 else "failed"
            await store.outbox_mark(str(row["id"]), status, error=tf.error.message)
            await log_event("retry", trace_id=trace_id, session_id=str(row["session_id"]),
                            agent="outbox",
                            payload={"tool": "send_lead_summary", "outcome": status,
                                     "attempts": row["attempts"] + 1,
                                     "error_code": tf.error.error_code})
    return sent
