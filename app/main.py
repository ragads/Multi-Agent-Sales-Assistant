"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
from __future__ import annotations
import html, json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agents.orchestrator import orchestrator
from app.config import settings
from app.jobs import sweeper
from app.mcp_client import hub
from app.observability.logger import log_event, new_trace_id
from app.security import (client_ip, limiter, require_admin, require_session_access,
                          valid_session_id)
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
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Admin-Token"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


class ChatIn(BaseModel):
    visitor_key: str = Field(min_length=8, max_length=100)
    message: str = Field(max_length=1500)
    visitor_tz: str | None = Field(default=None, max_length=64)


@app.get("/health")
async def health():
    have = {s["name"] for s in hub.schemas}
    calendar_ok = bool(have & {"propose_slots", "create_event"})
    email_ok = bool(have & {"send_lead_summary"})
    return {"status": "ok", "mcp_tools": [s["name"] for s in hub.schemas],
            "calendar_mcp": "up" if calendar_ok else "down",
            "email_mcp": "up" if email_ok else "down"}


@app.post("/api/mcp/reload", dependencies=[Depends(require_admin)])
async def reload_mcp():
    """Re-fetch tool schemas without restarting the API - use this if a server came up late."""
    schemas = await hub.load_schemas(retries=1)
    return {"status": "ok", "mcp_tools": [s["name"] for s in schemas]}


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    if not body.message.strip():
        raise HTTPException(400, "empty message")
    # rate limits: per visitor, per IP, and a global daily ceiling that protects the API budget
    if not (limiter.allow(f"v:{body.visitor_key}", settings.RATE_VISITOR_PER_MIN, 60)
            and limiter.allow(f"vd:{body.visitor_key}", settings.RATE_VISITOR_PER_DAY, 86400)
            and limiter.allow(f"ip:{client_ip(request)}", settings.RATE_IP_PER_MIN, 60)
            and limiter.allow("global", settings.RATE_GLOBAL_PER_DAY, 86400)):
        return JSONResponse(status_code=429, headers={"Retry-After": "30"}, content={
            "reply": "You are sending messages very quickly - please wait a moment and try again.",
            "rate_limited": True, "slots": []})
    try:
        return await orchestrator.handle(body.visitor_key, body.message, body.visitor_tz)
    except Exception as exc:  # FR-8.4: honest, human message - never a stack trace or a hang
        await log_event("error", trace_id=new_trace_id(), agent="api", payload={"detail": str(exc)})
        return {"reply": ("Something on our side failed just then and I don't want to give you a wrong "
                          "answer. Please email baskaran@closefuture.io and he'll pick it up today."),
                "error": True, "slots": []}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str, t: str | None = None,
                      x_admin_token: str | None = Header(default=None)):
    require_session_access(session_id, t, x_admin_token)
    state = await store.get(session_id)
    if state is None:
        raise HTTPException(404, "no such session")
    return state.model_dump()


@app.post("/api/session/{session_id}/end", dependencies=[Depends(require_admin)])
async def end_session(session_id: str, complete: bool = True):
    """Manual trigger used by the demo script for the happy-path lead email."""
    if not valid_session_id(session_id):
        raise HTTPException(404, "no such session")
    await orchestrator.finalize(session_id, complete=complete)
    state = await store.get(session_id)
    return {"status": "ok", "summary_sent": state.summary_sent if state else None}


@app.get("/api/trace/{session_id}")
async def trace(session_id: str, t: str | None = None,
                x_admin_token: str | None = Header(default=None)):
    require_session_access(session_id, t, x_admin_token)
    return {"session_id": session_id, "events": await store.trace(session_id)}


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(session_id: str, t: str | None = None,
                       x_admin_token: str | None = Header(default=None)):
    """Human-readable transcript + trace. The signed link in the lead email opens it (FR-6.3)."""
    require_session_access(session_id, t, x_admin_token)
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
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box}}
body{{font-family:system-ui,Arial;margin:clamp(12px,3vw,24px);background:#0f172a;color:#e2e8f0;
  -webkit-text-size-adjust:100%}}
h1,h2{{color:#93c5fd}} .m{{background:#1e293b;padding:10px 14px;border-radius:8px;margin:8px 0;max-width:780px}}
.m.visitor{{border-left:3px solid #38bdf8}} .m.assistant{{border-left:3px solid #4ade80}}
.ag{{float:right;color:#64748b;font-size:12px}} p{{margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;font-size:12px}} td{{border-top:1px solid #334155;padding:6px;vertical-align:top}}
pre{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;color:#94a3b8}} .t{{padding:2px 6px;border-radius:4px;background:#334155}}
.t.guardrail_check{{background:#7c2d12}} .t.tool_call{{background:#1e3a8a}} .t.routing_decision{{background:#065f46}}
.t.retry,.t.error,.t.fallback{{background:#7f1d1d}}
.meta{{color:#94a3b8;font-size:13px;overflow-wrap:anywhere}}
/* phones: the trace table stacks instead of scrolling sideways */
@media (max-width:640px){{
  h1{{font-size:22px}} h2{{font-size:17px}}
  .m{{max-width:none;padding:10px}}
  .ag{{float:none;display:block}}
  table,tbody,tr,td{{display:block;width:100%}}
  tr{{border-top:1px solid #334155;padding:8px 0}}
  td{{border:0;padding:2px 0}}
  td:nth-child(-n+4){{display:inline-block;width:auto;margin-right:8px;color:#94a3b8}}
  td:empty{{display:none}}
  pre{{font-size:11px}}
}}
</style>
<h1>Session {session_id[:8]}</h1>
<p class=meta>status <b>{state.status}</b> &middot; visitor {html.escape(state.visitor_key[:6])}&hellip; &middot;
tz {state.visitor_tz or '-'} &middot; booking {html.escape(json.dumps(state.booking or {}))} &middot;
summary {html.escape(json.dumps(state.summary_sent or {}))}</p>
<h2>Transcript</h2>{msgs}
<h2>End-to-end trace ({len(events)} events)</h2><table>{rows}</table>"""


@app.get("/widget", response_class=FileResponse)
async def widget():
    return FileResponse("widget/closefuture-chat.html")
