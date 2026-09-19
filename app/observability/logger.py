"""Structured logging: one row per event into `logs`, plus JSON on stdout (FR-3.9, FR-7.7, FR-8.5)."""
from __future__ import annotations
import json, logging, sys, time, uuid
from contextlib import contextmanager
from typing import Any, Optional

_stdout = logging.getLogger("closefuture")
if not _stdout.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _stdout.addHandler(h)
    _stdout.setLevel(logging.INFO)

EVENT_TYPES = {
    "routing_decision", "agent_call", "tool_call",
    "guardrail_check", "retry", "error", "fallback", "lifecycle",
}


def new_trace_id() -> str:
    return str(uuid.uuid4())


def _redact(payload: Any) -> Any:
    """Strip obvious PII from logged tool arguments."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k.lower() in {"email", "visitor_email", "phone", "attendee_email"} and isinstance(v, str):
                head, _, dom = v.partition("@")
                out[k] = (head[:2] + "***@" + dom) if dom else "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


async def log_event(
    event_type: str,
    *,
    trace_id: str,
    session_id: Optional[str] = None,
    agent: Optional[str] = None,
    payload: Optional[dict] = None,
    latency_ms: Optional[int] = None,
) -> None:
    from app.state.store import store  # local import avoids a cycle

    payload = _redact(payload or {})
    record = {
        "event_type": event_type, "trace_id": trace_id, "session_id": session_id,
        "agent": agent, "latency_ms": latency_ms, "payload": payload,
    }
    _stdout.info(json.dumps(record, default=str))
    try:
        await store.insert_log(event_type, trace_id, session_id, agent, payload, latency_ms)
    except Exception as exc:  # logging must never break the request
        _stdout.info(json.dumps({"event_type": "error", "detail": f"log write failed: {exc}"}))


@contextmanager
def timer():
    start = time.perf_counter()
    box = {"ms": 0}
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - start) * 1000)
