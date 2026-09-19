"""Background sweeper: idle timeout -> partial lead, plus session expiry (FR-3.7, FR-2.4, FR-6.4)."""
from __future__ import annotations
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.agents.orchestrator import orchestrator
from app.observability.logger import log_event, new_trace_id
from app.reliability.outbox import drain
from app.state.store import store

scheduler_ = AsyncIOScheduler()


async def sweep_idle() -> None:
    ids = await store.idle_sessions(settings.SESSION_IDLE_TIMEOUT_MIN)
    for session_id in ids:
        trace_id = new_trace_id()
        await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent="sweeper",
                        payload={"event": "idle_timeout",
                                 "after_minutes": settings.SESSION_IDLE_TIMEOUT_MIN,
                                 "action": "trigger partial lead summary"})
        await orchestrator.finalize(session_id, complete=False, trace_id=trace_id)


async def sweep_expired() -> None:
    n = await store.expire_sessions()
    if n:
        await log_event("lifecycle", trace_id=new_trace_id(), agent="sweeper",
                        payload={"event": "sessions_expired", "count": n})


async def sweep_outbox() -> None:
    sent = await drain()
    if sent:
        await log_event("lifecycle", trace_id=new_trace_id(), agent="sweeper",
                        payload={"event": "outbox_drained", "sent": sent})


def start() -> None:
    scheduler_.add_job(sweep_idle, "interval", seconds=60, id="idle", max_instances=1)
    scheduler_.add_job(sweep_outbox, "interval", seconds=120, id="outbox", max_instances=1)
    scheduler_.add_job(sweep_expired, "interval", hours=6, id="expiry", max_instances=1)
    scheduler_.start()


def stop() -> None:
    if scheduler_.running:
        scheduler_.shutdown(wait=False)
