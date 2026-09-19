"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
from __future__ import annotations
import html, json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from app.agents.orchestrator import orchestrator
from app.config import settings
from app.jobs import sweeper
from app.mcp_client import hub
from app.observability.logger import log_event, new_trace_id
from app.state.store import store


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.connect()
    schemas = await hub.load_schemas()
    await log_event("lifecycle", trace_id=new_trace_id(), agent="app",
                    payload={"event": "startup", "mcp_tools": [s["name"] for s in schemas]})
    sweeper.start()
    yield
    sweeper.stop()
    await store.close()


app = FastAPI(title="CloseFuture multi-agent assistant", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatIn(BaseModel):
    visitor_key: str
    message: str
    visitor_tz: str | None = None


@app.get("/health")
async def health():
    have = {s["name"] for s in hub.schemas}
    calendar_ok = bool(have & {"propose_slots", "create_event"})
    email_ok = bool(have & {"send_lead_summary"})
    return {"status": "ok", "mcp_tools": [s["name"] for s in hub.schemas],
            "calendar_mcp": "up" if calendar_ok else "down",
            "email_mcp": "up" if email_ok else "down"}


@app.post("/api/mcp/reload")
async def reload_mcp():
    """Re-fetch tool schemas without restarting the API - use this if a server came up late."""
    schemas = await hub.load_schemas(retries=1)
    return {"status": "ok", "mcp_tools": [s["name"] for s in schemas]}


@app.post("/api/chat")
async def chat(body: ChatIn):
    if not body.message.strip():
        raise HTTPException(400, "empty message")
    try:
        return await orchestrator.handle(body.visitor_key, body.message, body.visitor_tz)
    except Exception as exc:  # FR-8.4: honest, human message - never a stack trace or a hang
        await log_event("error", trace_id=new_trace_id(), agent="api", payload={"detail": str(exc)})
        return {"reply": ("Something on our side failed just then and I don't want to give you a wrong "
                          "answer. Please email baskaran@closefuture.io and he'll pick it up today."),
                "error": True, "slots": []}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    state = await store.get(session_id)
    if state is None:
        raise HTTPException(404, "no such session")
    return state.model_dump()


@app.post("/api/session/{session_id}/end")
async def end_session(session_id: str, complete: bool = True):
    """Manual trigger used by the demo script for the happy-path lead email."""
    await orchestrator.finalize(session_id, complete=complete)
    state = await store.get(session_id)
    return {"status": "ok", "summary_sent": state.summary_sent if state else None}


@app.get("/api/trace/{session_id}")
async def trace(session_id: str):
    return {"session_id": session_id, "events": await store.trace(session_id)}


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(session_id: str):
    """Human-readable transcript + trace. This URL is the link in the lead email (FR-6.3)."""
    state = await store.get(session_id)
    if state is None:
        raise HTTPException(404, "no such session")
    events = await store.trace(session_id)

    msgs = "".join(
        f"<div class='m {html.escape(m['role'])}'><b>{html.escape(m['role'])}</b>"
        f"<span class='ag'>{html.escape(m.get('agent') or '')}</span>"
        f"<p>{html.escape(m['content'])}</p></div>" for m in state.history
    )
    rows = "".join(
        f"<tr><td>{e['created_at']:%H:%M:%S}</td><td><span class='t {e['event_type']}'>"
        f"{e['event_type']}</span></td><td>{html.escape(e.get('agent') or '')}</td>"
        f"<td>{e.get('latency_ms') or ''}</td>"
        f"<td><pre>{html.escape(json.dumps(e['payload'] if not isinstance(e['payload'], str) else json.loads(e['payload']), indent=1, default=str)[:1600])}</pre></td></tr>"
        for e in events
    )
    return f"""<!doctype html><meta charset=utf-8><title>Session {session_id[:8]}</title>
<style>
body{{font-family:system-ui,Arial;margin:24px;background:#0f172a;color:#e2e8f0}}
h1,h2{{color:#93c5fd}} .m{{background:#1e293b;padding:10px 14px;border-radius:8px;margin:8px 0;max-width:780px}}
.m.visitor{{border-left:3px solid #38bdf8}} .m.assistant{{border-left:3px solid #4ade80}}
.ag{{float:right;color:#64748b;font-size:12px}} p{{margin:6px 0;white-space:pre-wrap}}
table{{border-collapse:collapse;width:100%;font-size:12px}} td{{border-top:1px solid #334155;padding:6px;vertical-align:top}}
pre{{margin:0;white-space:pre-wrap;color:#94a3b8}} .t{{padding:2px 6px;border-radius:4px;background:#334155}}
.t.guardrail_check{{background:#7c2d12}} .t.tool_call{{background:#1e3a8a}} .t.routing_decision{{background:#065f46}}
.t.retry,.t.error,.t.fallback{{background:#7f1d1d}}
.meta{{color:#94a3b8;font-size:13px}}
</style>
<h1>Session {session_id[:8]}</h1>
<p class=meta>status <b>{state.status}</b> &middot; visitor_key {html.escape(state.visitor_key)} &middot;
tz {state.visitor_tz or '-'} &middot; booking {html.escape(json.dumps(state.booking or {}))} &middot;
summary {html.escape(json.dumps(state.summary_sent or {}))}</p>
<h2>Transcript</h2>{msgs}
<h2>End-to-end trace ({len(events)} events)</h2><table>{rows}</table>"""


@app.get("/widget", response_class=FileResponse)
async def widget():
    return FileResponse("widget/closefuture-chat.html")
