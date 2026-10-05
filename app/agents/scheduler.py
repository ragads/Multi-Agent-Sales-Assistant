"""Scheduler agent: all calendar work through MCP tool calls (FR-5.1 .. FR-5.8).

The model decides WHAT to do (offer times, book, move, cancel, or ask a question). The code decides
the arguments that matter: `call_tool` below is the policy boundary between the model and the MCP
server. Code-owned arguments (visitor time zone, event id, idempotency key, slot end time) are hidden
from the model's tool schemas and filled in here; model-supplied ones (chosen slot, email, name) are
checked against what the server offered and what the visitor actually typed. A rejected call goes back
to the model as a structured tool error it can recover from, and never reaches the calendar.
"""
from __future__ import annotations
import json
from datetime import datetime

from app.booking_rules import email_given_by_visitor, match_offered_slot
from app.config import settings
from app.contracts import AgentRequest, AgentResponse, err
from app.llm import run_with_tools, to_openai_tools
from app.mcp_client import hub
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure

AGENT = "scheduler"

CAL_TOOLS = ["propose_slots", "check_availability", "create_event", "modify_event", "cancel_event"]

# arguments the model never sees or sets - the code supplies them from session state
CODE_OWNED = {
    "propose_slots": {"visitor_tz"},
    "create_event": {"idempotency_key", "end_iso"},
    "modify_event": {"event_id", "new_end_iso"},
    "cancel_event": {"event_id"},
}

# calendar-server rule violations the model can fix by picking another slot
RECOVERABLE = {"SLOT_TAKEN", "INVALID_DURATION", "OUTSIDE_BUSINESS_HOURS", "SLOT_IN_PAST", "MALFORMED_TIME"}


def model_schema(schema: dict) -> dict:
    """The tool schema as the model sees it: code-owned parameters removed."""
    hidden = CODE_OWNED.get(schema["name"], set())
    params = json.loads(json.dumps(schema.get("input_schema") or {"type": "object", "properties": {}}))
    params["properties"] = {k: v for k, v in (params.get("properties") or {}).items() if k not in hidden}
    if "required" in params:
        params["required"] = [k for k in params["required"] if k not in hidden]
    return {**schema, "input_schema": params}


def clamp(value, lo: int, hi: int, *, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def rejected(code: str, message: str) -> dict:
    return {"status": "error", "error_code": code, "retryable": False, "message": message}

SYS = """You are CloseFuture's scheduling assistant, booking 30-minute discovery calls with Baskaran.

You have real calendar tools. Use them - never invent times, never claim something is booked unless a
tool returned an event id.

Flow:
1. To offer times, call propose_slots with the visitor's IANA time zone. Present the slots using their
   visitor_label, numbered, and ask them to pick one.
2. Before create_event you need the visitor's name AND email. If either is missing, ask for it (one
   short question) and do not call the tool yet.
3. Once they pick a slot and you have name + email, call create_event with that slot's exact start_iso.
   Only slots returned by propose_slots can be booked.
4. To move an existing booking, offer new times with propose_slots, then call modify_event with the
   chosen slot's start_iso. To cancel, call cancel_event. The booking being changed is taken from the
   session automatically. Never create a second event for a visitor who already has one.
5. If a tool returns an error, read its error_code and message: ask for missing details
   (EMAIL_NOT_GIVEN), or apologise briefly and call propose_slots again (SLOT_TAKEN, SLOT_NOT_OFFERED,
   OUTSIDE_BUSINESS_HOURS and similar).

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
        tools = to_openai_tools([model_schema(s) for s in raw_tools])

        state_patch: dict = {}
        slots: list[dict] = []
        # slots offered this turn, else the ones offered last turn (persisted on the session)
        offered = lambda: slots or req.state.proposed_slots or []   # noqa: E731
        visitor_msgs = [m["content"] for m in req.state.history if m["role"] == "visitor"] + [req.message or ""]
        event_id = booking.get("event_id")

        async def guard(name: str, args: dict) -> dict | None:
            """Fill code-owned args and validate model-owned ones. Returns a rejection, or None if OK."""
            for k in CODE_OWNED.get(name, ()):
                args.pop(k, None)   # never trust a value the model was not meant to send

            if name == "propose_slots":
                args["visitor_tz"] = tz
                args["count"] = clamp(args.get("count"), 2, 3, default=3)
                args["days_ahead"] = clamp(args.get("days_ahead"), 1, 14, default=5)

            elif name == "create_event":
                if event_id:
                    return rejected("ALREADY_BOOKED", "this visitor already has a booking; use modify_event")
                slot = match_offered_slot(args.get("start_iso", ""), offered())
                if slot is None:
                    return rejected("SLOT_NOT_OFFERED", "only a slot returned by propose_slots can be booked")
                email = (args.get("visitor_email") or "").strip()
                if not email_given_by_visitor(email, visitor_msgs, qual.get("email")):
                    return rejected("EMAIL_NOT_GIVEN",
                                    "ask the visitor for their email address; only book with one they typed")
                name_ = qual.get("name") or (args.get("visitor_name") or "").strip()
                if not name_:
                    return rejected("NAME_NOT_GIVEN", "ask the visitor for their name")
                args.update(start_iso=slot["start_iso"], end_iso=slot["end_iso"], visitor_email=email,
                            visitor_name=name_[:100], notes=str(args.get("notes") or "")[:500],
                            idempotency_key=f"{req.session_id}:{slot['start_iso']}")

            elif name == "modify_event":
                if not event_id:
                    return rejected("NO_BOOKING", "there is no booking to move; offer new times instead")
                slot = match_offered_slot(args.get("new_start_iso", ""), offered())
                if slot is None:
                    return rejected("SLOT_NOT_OFFERED", "call propose_slots and move to one of those slots")
                args.update(event_id=event_id, new_start_iso=slot["start_iso"], new_end_iso=slot["end_iso"])

            elif name == "cancel_event":
                if not event_id:
                    return rejected("NO_BOOKING", "there is no booking to cancel")
                args["event_id"] = event_id
            return None

        async def call_tool(name: str, args: dict) -> dict:
            nonlocal slots, event_id
            if name not in CAL_TOOLS:
                return rejected("UNKNOWN_TOOL", f"{name} is not available")
            refusal = await guard(name, args)
            if refusal:
                await log_event("tool_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                                payload={"tool": name, "args": args, "rejected_by_policy": refusal})
                return refusal
            try:
                result = await hub.call(name, args, trace_id=req.trace_id,
                                        session_id=req.session_id, agent=AGENT)
            except ToolFailure as tf:
                if tf.error.error_code in RECOVERABLE:
                    return rejected(tf.error.error_code, tf.error.message or "pick another slot")
                raise

            if name == "propose_slots" and result.get("slots"):
                slots = result["slots"]
                state_patch["proposed_slots"] = slots
            if name == "create_event" and result.get("event_id"):
                event_id = result["event_id"]
                state_patch["booking"] = {
                    "event_id": result["event_id"], "start": result.get("start"),
                    "end": result.get("end"), "meet_link": result.get("meet_link"),
                    "html_link": result.get("html_link"),
                    "attendee_email": args.get("visitor_email"),
                    "visitor_label": (match_offered_slot(args["start_iso"], offered()) or {}).get("visitor_label"),
                }
                state_patch["status"] = "booked"
                state_patch["proposed_slots"] = []
            if name == "cancel_event" and result.get("cancelled"):
                event_id = None
                state_patch["booking"] = None
                state_patch["status"] = "active"
            if name == "modify_event" and result.get("event_id"):
                state_patch["booking"] = {
                    **booking, "event_id": result["event_id"], "start": result.get("start"),
                    "end": args["new_end_iso"], "meet_link": result.get("meet_link"),
                    "visitor_label": (match_offered_slot(args["new_start_iso"], offered()) or {}).get("visitor_label"),
                }
                state_patch["proposed_slots"] = []
            return result

        try:
            # No lock here: the Orchestrator holds the session lease for the whole turn (FR-2.6)
            scope = ("\n\nNOTE: the visitor's message also asks a question about CloseFuture. A colleague "
                     "answers it in this same reply, so handle ONLY the booking. Do not answer questions about "
                     "the company, timelines, prices or services, and do not repeat the question."
                     if req.params.get("question_handled_separately") else "")
            reply = await run_with_tools(SYS + scope + "\n\nSESSION STATE\n" + state_note,
                                         messages, tools, call_tool, max_tokens=700,
                                         name="scheduler.tools")
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
    # What the calendar actually returned, for the outbound guardrail: times and booking details in the
    # reply come from these tool results, not from the knowledge base, and must not read as invented.
    facts = []
    shown = slots or req.state.proposed_slots or []
    if shown:
        facts.append("Free slots returned by the calendar tool: " + "; ".join(
            f"{s.get('visitor_label')} (= {s.get('owner_label')})" for s in shown))
    if state_patch.get("booking"):
        facts.append("Booking result from the calendar tool: " + json.dumps(state_patch["booking"]))
    elif "booking" in state_patch:
        facts.append("The calendar tool cancelled the visitor's booking.")
    elif booking:
        facts.append("Existing booking on record: " + json.dumps(booking))
    return AgentResponse(agent=AGENT, output={"reply": reply, "slots": slots, "verified_facts": facts},
                         state_patch=state_patch, confidence=0.9)
