"""Scheduler agent: all calendar work through MCP tool calls (FR-5.1 .. FR-5.8)."""
from __future__ import annotations
import json
from datetime import datetime

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, err
from app.llm import run_with_tools, to_openai_tools
from app.mcp_client import hub
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure
from app.state.store import store

AGENT = "scheduler"

CAL_TOOLS = ["propose_slots", "check_availability", "create_event", "modify_event", "cancel_event"]

SYS = """You are CloseFuture's scheduling assistant, booking 30-minute discovery calls with Baskaran.

You have real calendar tools. Use them - never invent times, never claim something is booked unless a
tool returned an event id.

Flow:
1. As soon as the visitor wants to book, call propose_slots straight away (before asking for name or
   email) with the visitor's IANA time zone. Present the slots using their
   visitor_label, numbered, and ask them to pick one.
2. Before create_event you need the visitor's name AND email. If either is missing, ask for it (one
   short question) and do not call the tool yet.
3. Once they pick a slot and you have name + email, call create_event with that slot's exact start_iso
   and end_iso.
4. To move or cancel an existing booking, use the event_id from the session state given below - call
   modify_event or cancel_event. Never create a second event for a visitor who already has one.
5. If a tool returns SLOT_TAKEN, apologise briefly and call propose_slots again.

Keep replies to 2-3 sentences. Never mention tools, calendar APIs, or internal reasoning. Never promise
anything beyond the meeting itself."""


async def run(req: AgentRequest) -> AgentResponse:
    with timer() as t:
        tz = req.state.visitor_tz or req.params.get("visitor_tz") or settings.CALENDAR_OWNER_TZ
        booking = req.state.booking or {}
        qual = req.state.qualification or {}

        state_note = (
            f"Visitor time zone: {tz}\n"
            f"Calendar owner time zone: {settings.CALENDAR_OWNER_TZ}\n"
            f"Known name: {qual.get('name') or 'unknown'}\n"
            f"Known email: {qual.get('email') or 'unknown'}\n"
            f"Existing booking: {json.dumps(booking) if booking else 'none'}\n"
            f"Now: {datetime.now().isoformat(timespec='minutes')}"
        )
        history = [{"role": "assistant" if m["role"] != "visitor" else "user", "content": m["content"]}
                   for m in req.state.recent(8)]
        messages = history + [{"role": "user", "content": req.message or ""}]

        raw_tools = hub.schemas_for(CAL_TOOLS)
        if not raw_tools:
            return err(AGENT, "MCP_UNAVAILABLE", "calendar MCP server is not reachable", retryable=True)
        tools = to_openai_tools(raw_tools)

        state_patch: dict = {}
        slots: list[dict] = []

        async def call_tool(name: str, args: dict) -> dict:
            if name == "propose_slots":
                args.setdefault("visitor_tz", tz)
            if name == "create_event":
                args.setdefault("idempotency_key", f"{req.session_id}:{args.get('start_iso', '')}")
            try:
                result = await hub.call(name, args, trace_id=req.trace_id,
                                        session_id=req.session_id, agent=AGENT)
            except ToolFailure as tf:
                if tf.error.error_code == "SLOT_TAKEN":
                    return {"status": "error", "error_code": "SLOT_TAKEN",
                            "message": "slot no longer free, propose new ones"}
                raise

            nonlocal slots
            if name == "propose_slots" and result.get("slots"):
                slots = result["slots"]
            if name == "create_event" and result.get("event_id"):
                state_patch["booking"] = {
                    "event_id": result["event_id"], "start": result.get("start"),
                    "end": result.get("end"), "meet_link": result.get("meet_link"),
                    "html_link": result.get("html_link"),
                    "attendee_email": args.get("visitor_email"),
                    "visitor_label": next((s["visitor_label"] for s in slots
                                           if s["start_iso"] == args.get("start_iso")), None),
                }
                state_patch["status"] = "booked"
            if name == "cancel_event" and result.get("cancelled"):
                state_patch["booking"] = None
                state_patch["status"] = "active"
            if name == "modify_event" and result.get("event_id"):
                state_patch["booking"] = {**(req.state.booking or {}), "event_id": result["event_id"],
                                          "start": result.get("start"), "meet_link": result.get("meet_link")}
            return result

        try:
            # FR-2.6: hold the session advisory lock for the whole booking exchange
            async with store.session_lock(req.session_id):
                reply = await run_with_tools(SYS + "\n\nSESSION STATE\n" + state_note,
                                             messages, tools, call_tool, max_tokens=700)
        except ToolFailure as tf:
            # FR-8.1 fallback: collect details for manual follow-up instead of failing silently
            await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"reason": tf.error.error_code,
                                     "action": "manual_followup_collection"}, latency_ms=t["ms"])
            return AgentResponse(
                status="ok", agent=AGENT, error=tf.error,
                output={"reply": ("I couldn't reach our calendar just now, so I don't want to promise a "
                                  "slot I can't hold. If you leave your name, email and a rough time "
                                  "that suits you, Baskaran will confirm by email today."),
                        "manual_followup": True},
                state_patch={"booking": {"manual_followup": True, "reason": tf.error.error_code}},
            )
        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"detail": str(exc)})
            return err(AGENT, "SCHEDULER_FAILED", str(exc), retryable=True)

    await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                    payload={"slots_proposed": len(slots), "booked": "booking" in state_patch},
                    latency_ms=t["ms"])
    return AgentResponse(agent=AGENT, output={"reply": reply, "slots": slots},
                         state_patch=state_patch, confidence=0.9)
