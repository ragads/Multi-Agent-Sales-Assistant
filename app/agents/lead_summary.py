"""Lead-Summary agent: structured summary, deterministic score, exactly-once email (FR-6.1 .. FR-6.7).

Who decides what:
- the summary content and the score are built by code (extraction + app/scoring.py), so they are
  reproducible and cannot be rewritten by the model;
- WHETHER and HOW to notify sales is the model's decision, made through a tool call: it sees the new
  summary next to what was already sent and calls send_lead_summary, update_lead_summary, or nothing;
- exactly-once is enforced by code around that decision (outbox row + guards in call_tool), not by
  trusting the model.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, err
from app.llm import complete_json, run_with_tools
from app.mcp_client import hub
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure
from app.scoring import score_lead
from app.state.store import store

AGENT = "lead_summary"

EXTRACT_SYS = """You extract a sales lead summary from a website chat transcript.

Use only what the visitor actually said. Leave a field null rather than guessing. key_questions should
be the visitor's real questions, lightly cleaned up, max 6.

Keys: {"name":str|null,"email":str|null,"company":str|null,"project_type":str|null,
       "budget_hint":str|null,"timeline":str|null,"decision_role":str|null,
       "key_questions":[str],"next_step":str}
next_step: one sentence telling the sales rep what to do next."""

NOTIFY_SYS = """You are the Lead-Summary agent for CloseFuture. A website conversation has ended (or gone
idle) and you decide how to notify the sales team, using the email tools.

- If NO summary has been sent for this session yet, call send_lead_summary.
- If a summary WAS already sent, compare it with the new one. Call update_lead_summary only if the new
  summary gives sales something to act on that the earlier one lacked: a meeting booked, moved or
  cancelled, new contact details, a budget or timeline, or a change of tier. Otherwise call no tool.
- Call at most one tool. The summary itself is attached by the system - you only choose the action and
  give a short reason.

Finish with one sentence stating what you did and why."""

# The model's view of the email tools: an action plus a reason. The summary payload is code-owned.
NOTIFY_TOOLS = [
    {"type": "function", "function": {
        "name": "send_lead_summary",
        "description": "Email the lead summary to the sales inbox. Only for a session never reported before.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "update_lead_summary",
        "description": "Send an [UPDATED] summary for a session that was already reported.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}},
                       "required": ["reason"]}}},
]


async def build_summary(req: AgentRequest, complete_flag: bool) -> dict:
    transcript = req.state.transcript() or "(no messages)"
    try:
        data = await complete_json(EXTRACT_SYS, f"Transcript:\n{transcript}", max_tokens=900,
                                    name="lead_summary.extract")
    except Exception as exc:  # noqa: BLE001
        # The model being down must not lose the lead. Fall back to what the Orchestrator already
        # extracted turn by turn (session qualification) plus the visitor's own questions, verbatim.
        await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                        payload={"action": "summary_without_llm_extraction", "error": str(exc)[:300]})
        data = {"key_questions": [m["content"].strip() for m in req.state.history
                                  if m["role"] == "visitor" and m["content"].strip().endswith("?")]}

    qual = {**(req.state.qualification or {}),
            **{k: v for k, v in data.items() if k in
               {"name", "email", "company", "project_type", "budget_hint", "timeline", "decision_role"}
               and v}}
    questions = [q for q in (data.get("key_questions") or []) if isinstance(q, str)][:6]
    booking = req.state.booking or {}
    booked = bool(booking.get("event_id"))
    score, tier = score_lead(qual, booked, questions)

    return {
        "session_id": req.session_id,
        "visitor": {"name": qual.get("name"), "email": qual.get("email"),
                    "company": qual.get("company"), "timezone": req.state.visitor_tz},
        "key_questions": questions,
        "qualification": {"project_type": qual.get("project_type"),
                          "budget_hint": qual.get("budget_hint"),
                          "timeline": qual.get("timeline"),
                          "decision_role": qual.get("decision_role")},
        "lead_score": score,
        "tier": tier,
        "meeting": {"booked": booked,
                    "start_local": booking.get("visitor_label") or booking.get("start"),
                    "meet_link": booking.get("meet_link"),
                    "manual_followup": bool(booking.get("manual_followup"))},
        "complete": complete_flag,
        "conversation_url": f"{settings.APP_BASE_URL}/session/{req.session_id}",
        "next_step": data.get("next_step") or "Follow up by email.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _refuse(code: str, message: str) -> dict:
    return {"status": "error", "error_code": code, "retryable": False, "message": message}


async def run(req: AgentRequest) -> AgentResponse:
    """params: {'complete': bool}. Safe to call twice - the second call updates or skips, never duplicates."""
    with timer() as t:
        complete_flag = bool(req.params.get("complete", True))
        try:
            already = req.state.summary_sent
            summary = await build_summary(req, complete_flag)

            row_id, is_new, previous, row_status = await store.outbox_claim(req.session_id, summary)
            sent_before = bool(already) or row_status == "sent"

            # The first email is still queued from an earlier provider outage: the queued payload was
            # just refreshed with this newer summary and the outbox drain will send it. Nothing to decide.
            if not is_new and not sent_before:
                await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                                payload={"action": "refreshed_queued_summary", "outbox_status": row_status},
                                latency_ms=t["ms"])
                return AgentResponse(agent=AGENT, output={"queued": True, "summary": summary})

            chosen: dict = {}

            async def call_tool(name: str, args: dict) -> dict:
                # exactly-once guards (FR-6.6): the model chooses, the code decides what is allowed
                if chosen:
                    return _refuse("ONE_EMAIL_PER_TRIGGER", "an email was already sent for this trigger")
                if name == "send_lead_summary" and sent_before:
                    return _refuse("ALREADY_SENT", "this session was already reported; update or do nothing")
                if name == "update_lead_summary" and not sent_before:
                    return _refuse("NOTHING_SENT_YET", "no summary was sent yet; use send_lead_summary")
                if name not in {"send_lead_summary", "update_lead_summary"}:
                    return _refuse("UNKNOWN_TOOL", name)

                chosen.update(tool=name, reason=str(args.get("reason", ""))[:300])
                mcp_args: dict = {"summary": summary}   # code-owned payload, never model-written
                if name == "update_lead_summary":
                    mcp_args["previous_message_id"] = (already or {}).get("message_id") or ""
                chosen["result"] = await hub.call(name, mcp_args, trace_id=req.trace_id,
                                                  session_id=req.session_id, agent=AGENT)
                return {"status": "ok", "sent": True}

            decision_input = json.dumps({
                "already_sent": sent_before,
                "previous_summary": previous if sent_before else None,
                "new_summary": summary,
            }, default=str)

            try:
                note = await run_with_tools(NOTIFY_SYS, [{"role": "user", "content": decision_input}],
                                            NOTIFY_TOOLS, call_tool, max_tokens=200, max_turns=2,
                                            name="lead_summary.notify")
            except ToolFailure as tf:
                if sent_before:
                    # the lead already reached sales; a failed update is logged, not queued as a new send
                    await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                                    payload={"detail": "lead summary update failed",
                                             "error_code": tf.error.error_code})
                    return AgentResponse(status="error", agent=AGENT, error=tf.error,
                                         output={"summary": summary})
                # FR-8.1: first email failed (provider or model down) - the row stays pending, never lost
                await store.outbox_mark(row_id, "pending", error=tf.error.message,
                                        session_id=req.session_id)
                await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id,
                                agent=AGENT, payload={"action": "queued_for_retry",
                                                      "error_code": tf.error.error_code},
                                latency_ms=t["ms"])
                return AgentResponse(status="ok", agent=AGENT, error=tf.error,
                                     output={"queued": True, "summary": summary})

            if not chosen:
                if not sent_before:
                    # Safety net: a lead is never dropped because the model declined the first send.
                    # The outbox row stays pending and the drain delivers it.
                    await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id,
                                    agent=AGENT, payload={"action": "model_skipped_first_send_queued",
                                                          "model_note": note}, latency_ms=t["ms"])
                    return AgentResponse(agent=AGENT, output={"queued": True, "summary": summary})
                await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                                payload={"tool": None, "decision": "no_update_needed", "model_note": note,
                                         "score": summary["lead_score"], "tier": summary["tier"]},
                                latency_ms=t["ms"])
                return AgentResponse(agent=AGENT, output={"summary": summary, "tool": None})

            tool, result = chosen["tool"], chosen["result"]
            await store.outbox_mark(row_id, "sent", provider_id=result.get("provider_id"), payload=summary,
                                    session_id=req.session_id)
            sent_record = {"message_id": result.get("provider_id"),
                           "sent_at": datetime.now(timezone.utc).isoformat(),
                           "score": summary["lead_score"], "tier": summary["tier"],
                           "complete": complete_flag}

            await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"tool": tool, "reason": chosen["reason"], "model_note": note,
                                     "score": summary["lead_score"], "tier": summary["tier"],
                                     "complete": complete_flag, "duplicate_prevented": not is_new},
                            latency_ms=t["ms"])

            # FR-6.7: immutable completed-event record on the session (the store ignores it once set)
            return AgentResponse(agent=AGENT, output={"summary": summary, "tool": tool},
                                 state_patch={"summary_sent": sent_record})

        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"detail": str(exc)})
            return err(AGENT, "SUMMARY_FAILED", str(exc), retryable=True)
