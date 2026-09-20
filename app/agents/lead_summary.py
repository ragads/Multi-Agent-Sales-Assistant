"""Lead-Summary agent: structured summary, deterministic score, exactly-once email (FR-6.1 .. FR-6.7)."""
from __future__ import annotations
import json
from datetime import datetime, timezone

from app.contracts import AgentRequest, AgentResponse, err
from app.llm import complete_json
from app.mcp_client import hub
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure
from app.scoring import score_lead
from app.security import trace_link
from app.state.store import store

AGENT = "lead_summary"

EXTRACT_SYS = """You extract a sales lead summary from a website chat transcript.

Use only what the visitor actually said. Leave a field null rather than guessing. key_questions should
be the visitor's real questions, lightly cleaned up, max 6.

Keys: {"name":str|null,"email":str|null,"company":str|null,"project_type":str|null,
       "budget_hint":str|null,"timeline":str|null,"decision_role":str|null,
       "key_questions":[str],"next_step":str}
next_step: one sentence telling the sales rep what to do next."""


async def build_summary(req: AgentRequest, complete_flag: bool) -> dict:
    transcript = req.state.transcript() or "(no messages)"
    data = await complete_json(EXTRACT_SYS, f"Transcript:\n{transcript}", max_tokens=900)

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
        "conversation_url": trace_link(req.session_id),
        "next_step": data.get("next_step") or "Follow up by email.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def run(req: AgentRequest) -> AgentResponse:
    """params: {'complete': bool}. Safe to call twice - the second call updates, never duplicates."""
    with timer() as t:
        complete_flag = bool(req.params.get("complete", True))
        try:
            already = req.state.summary_sent
            summary = await build_summary(req, complete_flag)

            row_id, is_new = await store.outbox_claim(req.session_id, summary)

            # FR-6.6: a second trigger updates rather than sending a conflicting duplicate
            tool = "send_lead_summary" if (is_new and not already) else "update_lead_summary"
            args = {"summary": summary}
            if tool == "update_lead_summary":
                args["previous_message_id"] = (already or {}).get("message_id", "")

            try:
                result = await hub.call(tool, args, trace_id=req.trace_id,
                                        session_id=req.session_id, agent=AGENT)
            except ToolFailure as tf:
                # FR-8.1: queue for retry, never lose the lead
                await store.outbox_mark(row_id, req.session_id, "pending", error=tf.error.message)
                await log_event("fallback", trace_id=req.trace_id, session_id=req.session_id,
                                agent=AGENT, payload={"action": "queued_for_retry",
                                                      "error_code": tf.error.error_code},
                                latency_ms=t["ms"])
                return AgentResponse(status="ok", agent=AGENT, error=tf.error,
                                     output={"queued": True, "summary": summary})

            await store.outbox_mark(row_id, req.session_id, "sent", provider_id=result.get("provider_id"))
            sent_record = {"message_id": result.get("provider_id"),
                           "sent_at": datetime.now(timezone.utc).isoformat(),
                           "score": summary["lead_score"], "tier": summary["tier"],
                           "complete": complete_flag, "booked": summary["meeting"]["booked"]}

            await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"tool": tool, "score": summary["lead_score"],
                                     "tier": summary["tier"], "complete": complete_flag,
                                     "duplicate_prevented": not is_new}, latency_ms=t["ms"])

            # FR-6.7: immutable completed-event record on the session
            return AgentResponse(agent=AGENT, output={"summary": summary, "tool": tool},
                                 state_patch={"summary_sent": sent_record})

        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"detail": str(exc)})
            return err(AGENT, "SUMMARY_FAILED", str(exc), retryable=True)
