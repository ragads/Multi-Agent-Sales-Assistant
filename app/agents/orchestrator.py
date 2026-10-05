"""Orchestrator: classify -> route -> tools -> guardrail -> state (FR-3.1 .. FR-3.9).

A turn runs under the session's lease lock, so two messages for the same session are handled one after
the other while different sessions run fully in parallel. Sub-agents are called through _call_agent,
which turns any exception into an AgentError, so a failing agent is always reported back here as data
and never breaks the turn. See ARCHITECTURE.md for the full flow.
"""
from __future__ import annotations
import asyncio, json, traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, SessionState, err
from app.agents import guardrail, lead_summary, scheduler, search
from app.llm import bind_trace, complete_json
from app.observability.logger import log_event, new_trace_id, timer
from app.state.store import SessionBusyError, store

AGENT = "orchestrator"

CLASSIFY_SYS = """You are the router for CloseFuture's website assistant.

Classify the visitor's newest message into one or more intents:
- search: a question about CloseFuture (services, work, process, tech, pricing, company, founder)
- schedule: wants to book a call / see available times
- reschedule: wants to move an existing booking
- cancel: wants to cancel an existing booking
- provide_info: giving their name, email, company, budget or timeline
- smalltalk: greeting, thanks, chit-chat
- end_conversation: saying goodbye or that they are done

Set confidence honestly. A vague message like "can you help with the thing for my app?" is genuinely
ambiguous - give it LOW confidence rather than guessing a route.

Also extract qualification signals actually stated in this message (null otherwise) and rate
intent_strength 0-10 (how close this visitor sounds to buying).

Keys: {"intents":[{"intent":str,"confidence":float,"payload":{}}],
       "qualification_signals":{"name":null,"email":null,"company":null,"project_type":null,
                                "budget_hint":null,"timeline":null,"decision_role":null,
                                "intent_strength":0},
       "reasoning":str}"""

CLARIFY_SYS = """Write ONE short clarifying question (max 25 words) for a website visitor whose request
was ambiguous. Warm, specific, offering the two likeliest readings. No preamble.
Keys: {"question": str}"""

CLOSING_SYS = """Write a short, warm closing line (max 30 words) for a website visitor who is ending the
chat with CloseFuture, mentioning that Baskaran will follow up. No questions.
Keys: {"reply": str}"""

SMALLTALK_SYS = """You are CloseFuture's website assistant: an AI product studio that builds web and
mobile apps in 4-6 weeks. Reply to this greeting or chit-chat in at most two sentences and invite a
real question or a call. Never invent facts about the company.
Keys: {"reply": str}"""


# what the visitor sees when a sub-agent reports an error and produced no reply of its own (FR-8.4)
AGENT_FALLBACKS = {
    "search": ("I couldn't look that up just now, and I'd rather not guess. Could you ask again in a "
               "moment - or shall I set up a short call with Baskaran?"),
    "scheduler": ("I couldn't reach our calendar just now, so I don't want to promise a slot. If you leave "
                  "your name, email and a time that suits you, Baskaran will confirm by email today."),
}
GENERIC_FALLBACK = "Something on our side didn't work just then. Could you try that again in a moment?"


class Orchestrator:

    async def handle(self, visitor_key: str, message: str, visitor_tz: str | None = None) -> dict:
        trace_id = new_trace_id()
        state = await store.get_or_create_by_visitor_key(visitor_key, visitor_tz)
        with bind_trace(trace_id, state.id):
            try:
                # one turn at a time per session; other sessions are unaffected (FR-2.6)
                async with store.session_lock(state.id):
                    try:
                        return await self._turn(state.id, message, trace_id)
                    except Exception as exc:  # noqa: BLE001 - FR-8.4: an honest reply, never a silent gap
                        return await self._turn_failed(state.id, trace_id, exc)
            except SessionBusyError:
                await log_event("lifecycle", trace_id=trace_id, session_id=state.id, agent=AGENT,
                                payload={"event": "session_busy",
                                         "waited_s": settings.SESSION_LOCK_WAIT_S})
                return {"session_id": state.id, "trace_id": trace_id, "slots": [], "busy": True,
                        "reply": "I'm still working on your previous message - give me a moment and "
                                 "send that again."}

    async def _turn_failed(self, session_id: str, trace_id: str, exc: Exception) -> dict:
        """A turn crashed after the visitor's message was saved: record why, and answer honestly.

        The reply is also saved to the session, so a reload shows the visitor what happened instead of
        their own message with nothing after it.
        """
        reply = ("Something on our side failed just then and I don't want to give you a wrong answer. "
                 "Could you send that again? If it keeps happening, email baskaran@closefuture.io.")
        await log_event("error", trace_id=trace_id, session_id=session_id, agent=AGENT,
                        payload={"detail": f"turn failed: {type(exc).__name__}: {exc}",
                                 "where": traceback.format_exc(limit=6)[-1500:]})
        try:
            await store.append_message(session_id, "assistant", reply, agent="orchestrator:error")
        except Exception:  # noqa: BLE001 - the database may be what failed; the reply still goes out
            pass
        return {"session_id": session_id, "reply": reply, "trace_id": trace_id, "slots": [], "error": True}

    async def _call_agent(self, run: Callable[[AgentRequest], Awaitable[AgentResponse]],
                          req: AgentRequest, name: str) -> AgentResponse:
        """Handoff boundary: whatever the sub-agent does, the Orchestrator gets an AgentResponse back."""
        try:
            return await run(req)
        except Exception as exc:  # noqa: BLE001 - a crashing agent becomes a reported error, not a 500
            await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=name,
                            payload={"detail": f"unhandled in sub-agent: {exc}"})
            return err(name, f"{name.upper()}_CRASHED", str(exc), retryable=True)

    async def _turn(self, session_id: str, message: str, trace_id: str) -> dict:
        # re-read under the lease: a turn that just finished may have changed the state
        state = await store.get(session_id)
        req = AgentRequest(session_id=state.id, trace_id=trace_id, message=message, state=state)

        with timer() as turn:
            # ---- 1. inbound guardrail (FR-7.3) ----
            inbound = await guardrail.check_inbound(req)
            if inbound.verdict == "block":
                reply = inbound.safe_fallback or "I can't help with that, but I can tell you about CloseFuture."
                await store.append_message(state.id, "visitor", message)
                await store.append_message(state.id, "assistant", reply, agent="guardrail")
                await log_event("routing_decision", trace_id=trace_id, session_id=state.id, agent=AGENT,
                                payload={"chosen_agents": ["guardrail"], "blocked_inbound": True,
                                         "category": inbound.category, "reason": inbound.reason},
                                latency_ms=turn["ms"])
                return {"session_id": state.id, "reply": reply, "trace_id": trace_id,
                        "blocked": True, "slots": []}

            await store.append_message(state.id, "visitor", message)
            state = await store.get(state.id)
            req = AgentRequest(session_id=state.id, trace_id=trace_id, message=message, state=state)

            # ---- 2. classify (FR-3.2) ----
            try:
                cls = await complete_json(
                    CLASSIFY_SYS,
                    f"Conversation so far:\n{state.transcript() or '(none)'}\n\n"
                    f"Existing booking: {json.dumps(state.booking) if state.booking else 'none'}\n\n"
                    f"Newest message: {message}",
                    max_tokens=600, name="orchestrator.classify",
                )
            except Exception as exc:  # noqa: BLE001
                cls = {"intents": [{"intent": "search", "confidence": 0.5}],
                       "qualification_signals": {}, "reasoning": f"classifier failed: {exc}"}

            intents = [i for i in cls.get("intents", []) if i.get("intent")]
            intents.sort(key=lambda i: float(i.get("confidence", 0)), reverse=True)
            signals = {k: v for k, v in (cls.get("qualification_signals") or {}).items() if v}
            top_conf = float(intents[0].get("confidence", 0)) if intents else 0.0

            # ---- 3. route ----
            decision, responses, slots = await self._route(req, intents, top_conf)

            # ---- 3b. sub-agent failures come back as data (FR-8.3); never a silent empty reply ----
            agent_errors = []
            for r in responses:
                if r.error:
                    agent_errors.append(r.error.model_dump())
                if r.status == "error" and not r.reply:
                    r.output = {**(r.output or {}), "reply": AGENT_FALLBACKS.get(r.agent, GENERIC_FALLBACK)}
            if agent_errors:
                decision["agent_errors"] = agent_errors

            # ---- 4. compose the draft ----
            draft = "\n\n".join(r.reply for r in responses if r.reply).strip() or \
                "Could you tell me a little more about what you're looking for?"
            # everything the draft may legitimately state: retrieved passages, calendar tool output
            # (slots, booking results) and source names - the outbound guardrail judges against this
            context = "\n\n".join(
                [f for r in responses for f in (r.output or {}).get("verified_facts", [])]
                + [f"source: {c}" for r in responses for c in r.citations]
            )

            # ---- 5. outbound guardrail, owned here only (FR-3.8, FR-7.1) ----
            outbound = await guardrail.check_outbound(req, draft, context=context)
            parts = [r for r in responses if r.reply]
            if outbound.verdict == "block" and len(parts) > 1:
                # A multi-agent reply: find which part tripped the guardrail and replace only that part,
                # so one agent's bad sentence doesn't take the other agent's valid answer (e.g. real
                # calendar slots) down with it. Each part is judged against its own verified facts.
                kept, slots_ok = [], False
                for r in parts:
                    facts = "\n\n".join((r.output or {}).get("verified_facts", [])
                                        + [f"source: {c}" for c in r.citations])
                    v = await guardrail.check_outbound(req, r.reply, context=facts)
                    if v.verdict == "allow":
                        kept.append(r.reply)
                        slots_ok = slots_ok or bool((r.output or {}).get("slots"))
                    else:
                        kept.append(v.safe_fallback or outbound.safe_fallback)
                draft = "\n\n".join(kept)
                if not slots_ok:
                    slots = []
                decision["guardrail_partial"] = True
            elif outbound.verdict == "block":
                draft = outbound.safe_fallback or draft
                slots = []

            # ---- 6. merge state: only the Orchestrator writes, by applying sub-agent state_patches ----
            run_entry = {"agents": decision["chosen_agents"],
                         "at": datetime.now(timezone.utc).isoformat(),
                         "trace_id": trace_id}

            def build_patch(fresh: SessionState) -> dict:
                """Re-applied to FRESH state on every optimistic-lock retry, so nothing is overwritten."""
                patch: dict = {}
                qual = {**(fresh.qualification or {}), **signals}
                for r in responses:
                    for k, v in (r.state_patch or {}).items():
                        if k == "qualification":
                            qual.update(v or {})
                        else:
                            patch[k] = v
                if qual != (fresh.qualification or {}):
                    patch["qualification"] = qual
                patch["agents_run"] = (list(fresh.agents_run or []) + [run_entry])[-40:]
                return patch

            await store.append_message(state.id, "assistant", draft,
                                       agent=",".join(decision["chosen_agents"]) or "orchestrator")
            try:
                state = await store.update_with_retry(state.id, build_patch)
            except Exception as exc:  # noqa: BLE001
                await log_event("error", trace_id=trace_id, session_id=state.id, agent=AGENT,
                                payload={"detail": f"state update failed: {exc}"})

            # ---- 7. log the routing decision (FR-3.9) ----
            await log_event("routing_decision", trace_id=trace_id, session_id=state.id, agent=AGENT,
                            payload={**decision,
                                     "intents": intents,
                                     "classifier_reasoning": cls.get("reasoning"),
                                     "guardrail_outbound": outbound.verdict,
                                     "guardrail_category": outbound.category,
                                     "confidences": [r.confidence for r in responses]},
                            latency_ms=turn["ms"])

            # ---- 8. end of conversation? (FR-3.4) ----
            if decision.get("end_conversation") and not state.summary_sent:
                await self._finalize_locked(state.id, complete=True, trace_id=trace_id)   # lease already held

        return {"session_id": state.id, "reply": draft, "trace_id": trace_id,
                "slots": slots, "blocked": outbound.verdict == "block"}

    # ------------------------------------------------------------------ routing

    async def _route(self, req: AgentRequest, intents: list[dict], top_conf: float):
        names = [i["intent"] for i in intents]
        actionable = [n for n in names if n in {"search", "schedule", "reschedule", "cancel"}]
        decision = {"chosen_agents": [], "reason": "", "multi_intent_policy": None,
                    "end_conversation": False, "confidence": top_conf}

        # FR-3.6: low confidence -> clarify instead of routing to the wrong agent
        if actionable and top_conf < settings.INTENT_CONFIDENCE_FLOOR:
            q = await complete_json(CLARIFY_SYS, f"Ambiguous message: {req.message}", max_tokens=150,
                                    name="orchestrator.clarify")
            decision.update(chosen_agents=["orchestrator:clarify"],
                            reason=f"top intent confidence {top_conf:.2f} < "
                                   f"{settings.INTENT_CONFIDENCE_FLOOR}; asked a clarifying question")
            return decision, [AgentResponse(agent=AGENT, confidence=top_conf,
                                            output={"reply": q.get("question", "Could you say a bit more?")})], []

        if "end_conversation" in names and not actionable:
            r = await complete_json(CLOSING_SYS, f"Visitor said: {req.message}", max_tokens=150,
                                    name="orchestrator.close")
            decision.update(chosen_agents=["orchestrator:close"], reason="visitor ended the conversation",
                            end_conversation=True)
            return decision, [AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Thanks for stopping by!")})], []

        if not actionable:
            if "provide_info" in names:
                reply = ("Thanks - noted. Would you like me to answer anything else about CloseFuture, "
                         "or set up a short call with Baskaran?")
                decision.update(chosen_agents=["orchestrator:ack"], reason="visitor supplied details only")
                return decision, [AgentResponse(agent=AGENT, output={"reply": reply})], []
            r = await complete_json(SMALLTALK_SYS, f"Visitor said: {req.message}", max_tokens=200,
                                    name="orchestrator.smalltalk")
            decision.update(chosen_agents=["orchestrator:smalltalk"], reason="greeting or chit-chat")
            return decision, [AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Hello!")})], []

        # ---- multi-intent policy (FR-3.5), justification recorded per case ----
        sched_intents = [n for n in actionable if n in {"schedule", "reschedule", "cancel"}]
        has_search = "search" in actionable

        if has_search and sched_intents:
            decision["multi_intent_policy"] = "sequence"
            decision["reason"] = ("question + booking: answered first, then offered times, because the "
                                  "answer often changes what the visitor wants to book")
            # Search answers only the question half: the Scheduler offers real times in the same reply,
            # so Search must not improvise booking advice (invented links, "can't book" disclaimers)
            s1 = await self._call_agent(
                search.run, req.model_copy(update={"params": {**req.params, "booking_handled_separately": True}}),
                "search")
            # ...and the Scheduler handles only the booking half, so it never answers the question itself
            s2 = await self._call_agent(
                scheduler.run, req.model_copy(update={"params": {**req.params, "question_handled_separately": True}}),
                "scheduler")
            decision["chosen_agents"] = ["search", "scheduler"]
            slots = s2.output.get("slots", []) if s2.output else []
            return decision, [s1, s2], slots

        if len(sched_intents) > 1:
            decision["multi_intent_policy"] = "clarify"
            decision["reason"] = f"conflicting scheduling intents {sched_intents}; asked which one"
            q = await complete_json(CLARIFY_SYS, f"Conflicting request: {req.message}", max_tokens=150,
                                    name="orchestrator.clarify")
            decision["chosen_agents"] = ["orchestrator:clarify"]
            return decision, [AgentResponse(agent=AGENT, output={"reply": q.get("question", "Which would you like first?")})], []

        if actionable.count("search") > 1:
            decision["multi_intent_policy"] = "parallel"
            decision["reason"] = "two independent questions; retrieval is read-only so they run in parallel"
            payloads = [i.get("payload", {}).get("question") or req.message
                        for i in intents if i["intent"] == "search"][:2]
            reqs = [req.model_copy(update={"message": p}) for p in payloads]
            results = await asyncio.gather(*(self._call_agent(search.run, r, "search") for r in reqs))
            decision["chosen_agents"] = ["search", "search"]
            return decision, list(results), []

        if has_search:
            decision["chosen_agents"] = ["search"]
            decision["reason"] = "single question about the company"
            r = await self._call_agent(search.run, req, "search")
            # FR-4.7 escalation: a low-confidence answer offers the founder instead of guessing
            if r.confidence is not None and r.confidence < settings.CONFIDENCE_FLOOR and r.output:
                r.output["reply"] = r.output.get("reply", "") + (
                    "\n\nIf you need something more precise than that, a quick call with Baskaran is "
                    "the fastest way - shall I look at times?")
                decision["reason"] += f"; low confidence {r.confidence} -> offered escalation"
            return decision, [r], []

        decision["chosen_agents"] = ["scheduler"]
        decision["reason"] = f"scheduling intent: {sched_intents[0]}"
        r = await self._call_agent(scheduler.run, req, "scheduler")
        return decision, [r], (r.output.get("slots", []) if r.output else [])

    # ------------------------------------------------------------------ closing

    async def finalize(self, session_id: str, *, complete: bool, trace_id: str | None = None,
                       wait_s: float | None = None) -> None:
        """Trigger the Lead-Summary agent for this session (FR-3.4, FR-3.7, FR-6.4).

        Entry point for the sweeper and the /end API. Takes the session lease first so it can never run
        in the middle of a visitor's turn; the sweeper passes wait_s=0 and simply retries next sweep.
        A second trigger is safe: the Lead-Summary agent decides between update and no email.
        """
        trace_id = trace_id or new_trace_id()
        with bind_trace(trace_id, session_id):
            try:
                async with store.session_lock(session_id, wait_s=wait_s):
                    await self._finalize_locked(session_id, complete=complete, trace_id=trace_id)
            except SessionBusyError:
                await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent=AGENT,
                                payload={"event": "finalize_deferred", "reason": "session busy"})

    async def _finalize_locked(self, session_id: str, *, complete: bool, trace_id: str) -> None:
        state = await store.get(session_id)
        if state is None:
            return
        req = AgentRequest(session_id=session_id, trace_id=trace_id, state=state,
                           params={"complete": complete})
        res = await self._call_agent(lead_summary.run, req, "lead_summary")

        if res.status == "error" and not state.summary_sent:
            # Nothing was sent or queued. Leave the status alone so the sweeper tries again on its next
            # pass; marking the session completed/abandoned here would lose the lead for good.
            await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent=AGENT,
                            payload={"event": "finalize_failed_will_retry",
                                     "error_code": res.error.error_code if res.error else None})
            return

        def build_patch(fresh: SessionState) -> dict:
            patch = dict(res.state_patch or {})
            booked = bool((fresh.booking or {}).get("event_id"))
            patch["status"] = "completed" if (complete or booked) else "abandoned"
            return patch

        try:
            await store.update_with_retry(session_id, build_patch)
        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=trace_id, session_id=session_id, agent=AGENT,
                            payload={"detail": f"finalize state update failed: {exc}"})


orchestrator = Orchestrator()
