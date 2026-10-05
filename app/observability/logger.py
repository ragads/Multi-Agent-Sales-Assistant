"""Structured logging: one row per event into `logs`, plus JSON on stdout (FR-3.9, FR-7.7, FR-8.5)."""
from __future__ import annotations
import asyncio, json, logging, sys, time, uuid
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
    "guardrail_check", "retry", "error", "fallback", "lifecycle", "llm_call",
}


def new_trace_id() -> str:
    return str(uuid.uuid4())


_PII_KEYS = {"email", "visitor_email", "phone", "attendee_email"}
_URL_KEY_SUFFIXES = ("url", "link")


def _strip_query(url: str) -> str:
    """Drop the query string from a logged URL: manage_url and conversation_url carry their access
    token in ?t=, and an audit row is not a place to keep a working key to the thing it audits."""
    base, sep, _ = url.partition("?")
    return base + ("?<redacted>" if sep else "")


def _redact(payload: Any) -> Any:
    """Strip obvious PII and link tokens from logged tool arguments."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            key = k.lower()
            if (key.endswith(_URL_KEY_SUFFIXES) and isinstance(v, str)
                    and v.startswith(("http://", "https://"))):
                out[k] = _strip_query(v)
            elif key in _PII_KEYS and isinstance(v, str):
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
    payload = _redact(payload or {})
    record = {
        "event_type": event_type, "trace_id": trace_id, "session_id": session_id,
        "agent": agent, "latency_ms": latency_ms, "payload": payload,
    }
    _stdout.info(json.dumps(record, default=str))
    # The database write happens off the request path: each insert costs several network round trips
    # to Supabase, and a turn emits ~15 events. A single background writer keeps them in order.
    _ensure_writer()
    try:
        _queue.put_nowait((session_id, (event_type, trace_id, agent, payload, latency_ms)))
    except asyncio.QueueFull:  # logging must never block or break the request
        _stdout.info(json.dumps({"event_type": "error", "detail": "log queue full; row dropped"}))


_queue: Optional[asyncio.Queue] = None
_writer: Optional[asyncio.Task] = None


def _ensure_writer() -> None:
    global _queue, _writer
    loop = asyncio.get_running_loop()
    if _writer is None or _writer.done() or _writer.get_loop() is not loop:
        _queue = asyncio.Queue(maxsize=10_000)
        _writer = loop.create_task(_write_forever())


async def _write_forever() -> None:
    """Drain the queue in batches; rows are grouped per session (RLS scopes log writes by session)."""
    from app.state.store import store  # local import avoids a cycle

    while True:
        batch = [await _queue.get()]
        while not _queue.empty() and len(batch) < 200:
            batch.append(_queue.get_nowait())
        by_session: dict[Optional[str], list[tuple]] = {}
        for session_id, row in batch:   # dicts keep insertion order, so order within a session holds
            by_session.setdefault(session_id, []).append(row)
        for session_id, rows in by_session.items():
            try:
                await store.insert_logs(session_id, rows)
            except Exception as exc:  # noqa: BLE001
                _stdout.info(json.dumps({"event_type": "error", "detail": f"log write failed: {exc}"}))
        for _ in batch:
            _queue.task_done()


async def flush_logs() -> None:
    """Wait until every queued log row is written. Called on shutdown (store.close)."""
    if _queue is not None and _writer is not None and not _writer.done() \
            and _writer.get_loop() is asyncio.get_running_loop():
        await _queue.join()


@contextmanager
def timer():
    start = time.perf_counter()
    box = {"ms": 0}
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - start) * 1000)
