"""Scheduler agent: all calendar work through MCP tool calls (FR-5.1 .. FR-5.8)."""
from __future__ import annotations
import json, re
from datetime import datetime

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, err
from app.llm import run_with_tools, to_openai_tools
from app.mcp_client import hub
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure
from app.security import manage_link
from app.state.store import store

AGENT = "scheduler"

CAL_TOOLS = ["propose_slots", "check_availability", "create_event", "modify_event", "cancel_event"]

# Excludes the delimiters a mail header treats specially, so an address like "<a@b.com>" or
# "a@b.com, victim@c.com" cannot be smuggled into the attendee field as one value.
_EMAIL_RE = re.compile(r"[^@\s<>,;:\"']+@[^@\s<>,;:\"']+\.[^@\s<>,;:\"']+")


def _visitor_emails(state) -> set[str]:
    """Addresses this visitor typed themselves, plus the one already recorded on the session.

    The classifier's signals for the CURRENT turn are merged into qualification only after routing,
    so on the turn where someone first gives their address it exists in the history and nowhere
    else - hence both sources. This stops the model inventing or substituting an address; it does
    not stop a visitor from typing someone else's, which is bounded by the rate limits instead.
    """
    seen = {e.lower() for m in state.history if m["role"] == "visitor"
            for e in _EMAIL_RE.findall(m["content"])}
    known = ((state.qualification or {}).get("email") or "").strip().lower()
    if known:
        seen.add(known)
    return seen


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

Write plain text only. The chat widget renders replies literally, so markdown asterisks show up as
asterisks - never use **bold**, *italic* or * bullets. Number slots as "1." "2." "3." and nothing more.

Never announce that you are about to check availability. Saying "let me check available slots"
and stopping leaves the visitor waiting for something that is not coming - call propose_slots in
this same turn and present what it returns.

The visitor's time zone is already given to you under SESSION STATE, and it is filled in for you
whenever you call propose_slots. NEVER ask the visitor what time zone they are in - you have it,
and asking stalls the booking for no reason.

When the visitor also asked about the company or its pricing, another agent is answering that
part in the same reply as yours. Say nothing at all about pricing, rates or turnaround - do not
repeat them, and do not disclaim them either. "I can't provide pricing details" directly
contradicts the answer printed beside yours, which has just given them. Write only your half:
the times and what you need to book one.

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
                # The invite carries a signed link back to this booking, so the visitor can move or
                # cancel it themselves instead of emailing and waiting.
                args.setdefault("manage_url", manage_link(req.session_id))
                # The invite goes to an address this visitor actually typed, not to one the model
                # composed. Without this a prompt-injected turn can send a calendar invite from the
                # owner's Google account to any address it cares to name.
                supplied = (args.get("visitor_email") or "").strip()
                if not _EMAIL_RE.fullmatch(supplied) or supplied.lower() not in _visitor_emails(req.state):
                    return {"status": "error", "error_code": "NO_VISITOR_EMAIL",
                            "message": "ask the visitor for their own email address before booking"}
                args["visitor_email"] = supplied
            if name in {"modify_event", "cancel_event"}:
                # Authorisation, not prompt guidance. These move or delete a real calendar entry,
                # so the id comes from this session's own booking and whatever the model passed is
                # discarded - an injected event id now changes nothing. The SYS prompt asks for the
                # same thing, but a sentence of English is not an access control.
                event_id = (req.state.booking or {}).get("event_id")
                if not event_id:
                    return {"status": "error", "error_code": "NO_BOOKING",
                            "message": "this session has no booking to change"}
                args["event_id"] = event_id
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
                # no same-day promise here: the spec treats a contractual timeline as an
                # unauthorised commitment, and the outbound guardrail correctly flagged the old wording
                output={"reply": ("I couldn't reach our calendar just now, so I don't want to promise a "
                                  "slot I can't hold. If you leave your name, email and a rough time "
                                  "that suits you, Baskaran will follow up by email to confirm."),
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


async def propose_only(req: AgentRequest) -> AgentResponse:
    """Fetch times and say one fixed line - no model, no prose.

    On a "what do you charge and can I book a call?" turn the Search agent and this one both write
    into the same reply, and three rounds of prompt wording failed to stop the second model
    restating the pricing the first had just given, or asking for a time zone it already had, or
    asking for a name before it had called propose_slots at all. Two models cannot be reliably
    talked into writing one coherent answer between them. So on that path this agent stops writing:
    it calls the tool and returns a fixed sentence, and the slots become buttons as usual. The full
    conversational agent still runs for every turn that is only about booking.
    """
    with timer() as t:
        tz = req.state.visitor_tz or req.params.get("visitor_tz") or settings.CALENDAR_OWNER_TZ
        try:
            result = await hub.call("propose_slots", {"visitor_tz": tz}, trace_id=req.trace_id,
                                    session_id=req.session_id, agent=AGENT)
        except ToolFailure as tf:
            await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"reason": tf.error.error_code, "action": "manual_followup_collection"},
                            latency_ms=t["ms"])
            return AgentResponse(
                status="ok", agent=AGENT, error=tf.error,
                output={"reply": ("I couldn't reach our calendar just now, so I don't want to promise a "
                                  "slot I can't hold. Leave your name and email and Baskaran will "
                                  "follow up to confirm a time."),
                        "manual_followup": True},
                state_patch={"booking": {"manual_followup": True, "reason": tf.error.error_code}})

        slots = result.get("slots") or []
        reply = ("Here are the next open times - pick one and I'll book it." if slots else
                 "I don't have an open slot in the next few days. Tell me roughly when suits you and "
                 "I'll find one.")

    await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                    payload={"slots_proposed": len(slots), "booked": False, "mode": "propose_only"},
                    latency_ms=t["ms"])
    return AgentResponse(agent=AGENT, output={"reply": reply, "slots": slots}, confidence=0.9)
