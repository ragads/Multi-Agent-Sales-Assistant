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


# --------------------------------------------------------------------- self-service booking (FR-5.8)
# The calendar invite carries a signed link here, so a visitor can move or cancel their own call
# without emailing and waiting for a reply. The token is the same HMAC that guards /session.
#
# Every mutation is a POST. Mail clients and link scanners routinely fetch the URLs in a message to
# build previews, and a cancel that acted on GET would delete real bookings on its own.

class RescheduleIn(BaseModel):
    start_iso: str = Field(max_length=40)
    end_iso: str = Field(max_length=40)


async def _booking_or_404(session_id: str, t: str | None):
    require_session_access(session_id, t, None)
    state = await store.get(session_id)
    booking = (state.booking or {}) if state else {}
    if not booking.get("event_id"):
        raise HTTPException(404, "no booking on this session")
    return state, booking


@app.get("/booking/{session_id}", response_class=HTMLResponse)
async def booking_page(session_id: str, t: str | None = None):
    _state, booking = await _booking_or_404(session_id, t)
    when = booking.get("visitor_label") or booking.get("start") or "your booked time"
    meet = booking.get("meet_link") or ""
    meet_html = f'<a class=meet href="{html.escape(meet)}">{html.escape(meet)}</a>' if meet else ""
    return f"""<!doctype html><meta charset=utf-8><title>Your CloseFuture call</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box}}
body{{font-family:system-ui,Arial;margin:0;padding:clamp(16px,5vw,48px);background:#0f1f00;color:#e9f3d4}}
.card{{max-width:560px;margin:0 auto;background:#16290a;border:1px solid #2f4d16;border-radius:14px;
  padding:clamp(18px,4vw,28px)}}
h1{{margin:0 0 4px;font-size:21px;color:#c6e78a}} .when{{font-size:17px;margin:14px 0 6px}}
a.meet{{color:#9fd356;overflow-wrap:anywhere}}
.row{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}
button{{font:inherit;padding:11px 16px;border-radius:9px;border:1px solid #2f4d16;cursor:pointer;
  background:#1f3a0d;color:#e9f3d4}}
button.primary{{background:#4ade80;color:#0f1f00;border-color:#4ade80;font-weight:600}}
button:disabled{{opacity:.5;cursor:default}}
.slot{{display:block;width:100%;text-align:left;margin:8px 0}}
#msg{{margin-top:18px;padding:12px;border-radius:9px;background:#1f3a0d;display:none}}
.muted{{color:#9bb07a;font-size:13px;margin-top:18px}}
</style>
<div class=card>
  <h1>Your CloseFuture call</h1>
  <div class=when id=when>{html.escape(str(when))}</div>
  {meet_html}
  <div class=row>
    <button class=primary id=resch>Pick a new time</button>
    <button id=cancel>Cancel this call</button>
  </div>
  <div id=slots></div>
  <div id=msg></div>
  <p class=muted>This link is personal to your booking - please don't forward it.</p>
</div>
<script>
const SID = {json.dumps(session_id)}, T = {json.dumps(t or "")};
const msg = document.getElementById("msg"), slots = document.getElementById("slots");
function say(text, done){{
  msg.style.display = "block"; msg.textContent = text;
  if(done){{ document.getElementById("resch").disabled = true;
             document.getElementById("cancel").disabled = true; slots.innerHTML = ""; }}
}}
document.getElementById("resch").onclick = async () => {{
  say("Finding open times...");
  const r = await fetch("/api/booking/" + SID + "/slots?t=" + encodeURIComponent(T));
  if(!r.ok) return say("Couldn't load times just now. Please email baskaran@closefuture.io.");
  const data = await r.json();
  msg.style.display = "none"; slots.innerHTML = "";
  if(!data.slots.length) return say("No open slots in the next few days - email baskaran@closefuture.io.");
  data.slots.forEach(s => {{
    const b = document.createElement("button");
    b.className = "slot"; b.textContent = s.visitor_label || s.start_iso;
    b.onclick = async () => {{
      say("Moving your call...");
      const res = await fetch("/api/booking/" + SID + "/reschedule?t=" + encodeURIComponent(T), {{
        method: "POST", headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{start_iso: s.start_iso, end_iso: s.end_iso}})
      }});
      const out = await res.json().catch(() => ({{}}));
      if(res.ok && out.status === "ok"){{
        document.getElementById("when").textContent = s.visitor_label || s.start_iso;
        say("Done - your call has been moved. A new invite is on its way.", true);
      }} else say(out.detail || "That didn't work. Please email baskaran@closefuture.io.");
    }};
    slots.appendChild(b);
  }});
}};
document.getElementById("cancel").onclick = async () => {{
  if(!confirm("Cancel this call?")) return;
  say("Cancelling...");
  const res = await fetch("/api/booking/" + SID + "/cancel?t=" + encodeURIComponent(T), {{method: "POST"}});
  const out = await res.json().catch(() => ({{}}));
  if(res.ok && out.status === "ok")
    say("Your call is cancelled. You're welcome to book again any time.", true);
  else say(out.detail || "That didn't work. Please email baskaran@closefuture.io.");
}};
</script>"""


@app.get("/api/booking/{session_id}/slots")
async def booking_slots(session_id: str, t: str | None = None):
    state, _booking = await _booking_or_404(session_id, t)
    tz = state.visitor_tz or settings.CALENDAR_OWNER_TZ
    try:
        result = await hub.call("propose_slots", {"visitor_tz": tz}, trace_id=new_trace_id(),
                                session_id=session_id, agent="self_serve")
    except Exception:
        raise HTTPException(503, "calendar unavailable")
    return {"slots": result.get("slots") or []}


@app.post("/api/booking/{session_id}/reschedule")
async def booking_reschedule(session_id: str, body: RescheduleIn, t: str | None = None):
    state, booking = await _booking_or_404(session_id, t)
    trace_id = new_trace_id()
    try:
        result = await hub.call(
            "modify_event",
            {"event_id": booking["event_id"], "new_start_iso": body.start_iso,
             "new_end_iso": body.end_iso},
            trace_id=trace_id, session_id=session_id, agent="self_serve")
    except Exception as exc:
        await log_event("error", trace_id=trace_id, session_id=session_id, agent="self_serve",
                        payload={"detail": f"self-serve reschedule failed: {exc}"})
        raise HTTPException(503, "could not move the booking")
    # visitor_label described the old time, so it is dropped rather than left contradicting the start
    patch = {"booking": {**booking, "start": result.get("start", body.start_iso),
                         "meet_link": result.get("meet_link") or booking.get("meet_link"),
                         "visitor_label": None}}
    await store.update_with_retry(session_id, patch, state.version)
    await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent="self_serve",
                    payload={"event": "rescheduled_by_visitor", "start": body.start_iso})
    return {"status": "ok", "start": result.get("start", body.start_iso)}


@app.post("/api/booking/{session_id}/cancel")
async def booking_cancel(session_id: str, t: str | None = None):
    state, booking = await _booking_or_404(session_id, t)
    trace_id = new_trace_id()
    try:
        await hub.call("cancel_event", {"event_id": booking["event_id"]}, trace_id=trace_id,
                       session_id=session_id, agent="self_serve")
    except Exception as exc:
        await log_event("error", trace_id=trace_id, session_id=session_id, agent="self_serve",
                        payload={"detail": f"self-serve cancel failed: {exc}"})
        raise HTTPException(503, "could not cancel the booking")
    await store.update_with_retry(session_id, {"booking": None, "status": "active"}, state.version)
    await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent="self_serve",
                    payload={"event": "cancelled_by_visitor", "event_id": booking["event_id"]})
    return {"status": "ok"}
