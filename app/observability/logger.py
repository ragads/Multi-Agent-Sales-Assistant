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


_PII_KEYS = {"email", "visitor_email", "phone", "attendee_email"}
_URL_KEY_SUFFIXES = ("url", "link")


def _strip_query(url: str) -> str:
    """Drop the query string from a logged URL.

    manage_url and conversation_url carry their access token in ?t=. An audit row is not a place
    to keep a working key to the thing it audits, and these rows go to the logs table and to
    stdout, which on a hosted runtime means the platform log viewer and anything shipping from it.
    """
    base, sep, _ = url.partition("?")
    return base + ("?<redacted>" if sep else "")


def _redact(payload: Any) -> Any:
    """Strip obvious PII and link tokens from logged tool arguments."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            key = k.lower()
            if key in _PII_KEYS and isinstance(v, str):
                head, _, dom = v.partition("@")
                out[k] = (head[:2] + "***@" + dom) if dom else "***"
            elif (key.endswith(_URL_KEY_SUFFIXES) and isinstance(v, str)
                  and v.startswith(("http://", "https://"))):
                out[k] = _strip_query(v)
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
