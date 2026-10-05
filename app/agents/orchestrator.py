"""Orchestrator: classify -> route -> tools -> guardrail -> state (FR-3.1 .. FR-3.9).

A turn runs under the session's lease lock, so two messages for the same session are handled one after
the other while different sessions run fully in parallel. Sub-agents are called through _call_agent,
which turns any exception into an AgentError, so a failing agent is always reported back here as data
and never breaks the turn. See ARCHITECTURE.md for the full flow.
"""
from __future__ import annotations
import asyncio, json, re, time, traceback
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, SessionState, err
from app.agents import guardrail, lead_summary, scheduler, search
from app.agents.routing import decide_route
from app.llm import bind_trace, complete_json
from app.observability.logger import log_event, new_trace_id, timer
from app.state.store import SessionBusyError, store

AGENT = "orchestrator"
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\((\S+?)\)")   # [text](url) -> url

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
real question or a call. Never invent facts about the company. Never say you have booked, moved,
cancelled or sent anything - you take no actions; if the visitor asks for one, say you'll need a moment
to look at it and ask them to confirm what they'd like.
Keys: {"reply": str}"""


# what the visitor sees when a sub-agent reports an error and produced no reply of its own (FR-8.4)
AGENT_FALLBACKS = {
    "search": ("I couldn't look that up just now, and I'd rather not guess. Could you ask again in a "
               "moment - or shall I set up a short call with Baskaran?"),
    "scheduler": ("I couldn't reach our calendar just now, so I don't want to promise a slot. If you leave "
                  "your name, email and a time that suits you, Baskaran will confirm by email today."),
}
# A lead worth mailing needs something the recipient can act on; a name alone is not that.
REPORTABLE_SIGNALS = ("email", "company", "project_type", "budget_hint", "timeline")


def _worth_reporting(state: SessionState) -> bool:
    qual = state.qualification or {}
    return any(qual.get(k) for k in REPORTABLE_SIGNALS)


def booked_now(r: AgentResponse) -> dict | None:
    """The booking a sub-agent just created this turn (an event id came back from the calendar)."""
    b = (r.state_patch or {}).get("booking") or {}
    return b if b.get("event_id") else None


def _booking_confirmation(b: dict) -> str:
    """What was actually booked, used if the guardrail blocks the model's own confirmation."""
    when = b.get("visitor_label") or b.get("start") or "the time you chose"
    line = f"You're booked for {when}."
    if b.get("attendee_email"):
        line += f" The calendar invite is on its way to {b['attendee_email']}."
    if b.get("meet_link"):
        line += f" Google Meet link: {b['meet_link']}"
    return line + " If anything looks wrong, email baskaran@closefuture.io."


GENERIC_FALLBACK ="Something on our side didn't work just then. Could you try that again in a moment?"


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

    # ------------------------------------------------------------------ one turn
    # A turn is a fixed sequence of steps over one dict of turn state. The steps are shared by both
    # orchestration engines: the native pipeline below calls them in order, and the LangGraph engine
    # (app/agents/graph.py) runs each one as a graph node, with the routing policy as conditional edges.
    # Each step takes the turn state and returns only the keys it adds or changes.

    async def _turn(self, session_id: str, message: str, trace_id: str) -> dict:
        t = {"session_id": session_id, "message": message, "trace_id": trace_id,
             "t0": time.perf_counter(), "path": []}
        if settings.ORCHESTRATOR_ENGINE == "langgraph":
            from app.agents.graph import turn_graph   # local import: graph.py imports this module
            out = await turn_graph.ainvoke(t)
            return out["result"]

        # native engine: the same steps, called in order
        t.update(await self.step_inbound(t))
        if t.get("result"):
            return t["result"]
        t.update(await self.step_classify(t))
        t.update(decide_route(t["intents"], t["top_conf"]))
        t.update(await self.run_route(t["route"], t))
        t.update(await self.step_compose(t))
        t.update(await self.step_guardrail(t))
        t.update(await self.step_persist(t))
        return t["result"]

    @staticmethod
    def _elapsed_ms(t: dict) -> int:
        return int((time.perf_counter() - t["t0"]) * 1000)

    async def step_inbound(self, t: dict) -> dict:
        """Load fresh state under the lease, screen the message (FR-7.3), save it."""
        state = await store.get(t["session_id"])   # re-read under the lease: a previous turn may have changed it
        req = AgentRequest(session_id=state.id, trace_id=t["trace_id"], message=t["message"], state=state)
        inbound = await guardrail.check_inbound(req)
        if inbound.verdict == "block":
            reply = inbound.safe_fallback or "I can't help with that, but I can tell you about CloseFuture."
            await store.append_message(state.id, "visitor", t["message"])
            await store.append_message(state.id, "assistant", reply, agent="guardrail")
            await log_event("routing_decision", trace_id=t["trace_id"], session_id=state.id, agent=AGENT,
                            payload={"chosen_agents": ["guardrail"], "blocked_inbound": True,
                                     "category": inbound.category, "reason": inbound.reason,
                                     "engine": settings.ORCHESTRATOR_ENGINE},
                            latency_ms=self._elapsed_ms(t))
            return {"result": {"session_id": state.id, "reply": reply, "trace_id": t["trace_id"],
                               "blocked": True, "slots": []}}
        await store.append_message(state.id, "visitor", t["message"])
        state = await store.get(state.id)
        return {"state": state,
                "req": AgentRequest(session_id=state.id, trace_id=t["trace_id"], message=t["message"], state=state)}

    async def step_classify(self, t: dict) -> dict:
        """Ranked intents plus qualification signals (FR-3.2)."""
        state, message = t["state"], t["message"]
        cls, intents = {}, []
        # Two tries: small models occasionally return empty or malformed JSON, and a message with
        # no intent must never fall through to small talk - "cancel my booking" would get a chatty
        # reply that claims an action nobody took.
        for _attempt in range(2):
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
            intents = [i for i in (cls.get("intents") or []) if isinstance(i, dict) and i.get("intent")]
            if intents:
                break
        if not intents:
            # still nothing usable: ask, rather than guess (FR-3.6)
            intents = [{"intent": "search", "confidence": 0.0}]
            cls["reasoning"] = (cls.get("reasoning") or "") + " | classifier returned no intents twice"
        intents.sort(key=lambda i: float(i.get("confidence", 0)), reverse=True)
        return {"cls": cls, "intents": intents,
                "signals": {k: v for k, v in (cls.get("qualification_signals") or {}).items() if v},
                "top_conf": float(intents[0].get("confidence", 0)) if intents else 0.0}

    # ---- route steps: one per route in app/agents/routing.py; each returns decision/responses/slots

    async def run_route(self, route: str, t: dict) -> dict:
        return await getattr(self, f"route_{route}")(t)

    async def route_clarify(self, t: dict) -> dict:
        """FR-3.6: low confidence -> ask a clarifying question instead of routing to the wrong agent."""
        req, decision = t["req"], dict(t["decision"])
        q = await complete_json(CLARIFY_SYS, f"Ambiguous message: {req.message}", max_tokens=150,
                                name="orchestrator.clarify")
        decision.update(chosen_agents=["orchestrator:clarify"],
                        reason=f"top intent confidence {t['top_conf']:.2f} < "
                               f"{settings.INTENT_CONFIDENCE_FLOOR}; asked a clarifying question")
        return {"decision": decision, "slots": [], "responses": [
            AgentResponse(agent=AGENT, confidence=t["top_conf"],
                          output={"reply": q.get("question", "Could you say a bit more?")})]}

    async def route_close(self, t: dict) -> dict:
        req, decision = t["req"], dict(t["decision"])
        r = await complete_json(CLOSING_SYS, f"Visitor said: {req.message}", max_tokens=150,
                                name="orchestrator.close")
        decision.update(chosen_agents=["orchestrator:close"], reason="visitor ended the conversation",
                        end_conversation=True)
        return {"decision": decision, "slots": [], "responses": [
            AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Thanks for stopping by!")})]}

    async def route_ack(self, t: dict) -> dict:
        decision = dict(t["decision"])
        decision.update(chosen_agents=["orchestrator:ack"], reason="visitor supplied details only")
        reply = ("Thanks - noted. Would you like me to answer anything else about CloseFuture, "
                 "or set up a short call with Baskaran?")
        return {"decision": decision, "slots": [], "responses": [AgentResponse(agent=AGENT, output={"reply": reply})]}

    async def route_smalltalk(self, t: dict) -> dict:
        req, decision = t["req"], dict(t["decision"])
        r = await complete_json(SMALLTALK_SYS, f"Visitor said: {req.message}", max_tokens=200,
                                name="orchestrator.smalltalk")
        decision.update(chosen_agents=["orchestrator:smalltalk"], reason="greeting or chit-chat")
        return {"decision": decision, "slots": [], "responses": [
            AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Hello!")})]}

    async def route_sequence(self, t: dict) -> dict:
        """Question + booking (FR-3.5): answer first, then offer times, in one reply."""
        req, intents, decision = t["req"], t["intents"], dict(t["decision"])
        sched_intents = [i["intent"] for i in intents if i["intent"] in {"schedule", "reschedule", "cancel"}]
        decision["multi_intent_policy"] = "sequence"
        decision["reason"] = ("question + booking: answered first, then offered times, because the "
                              "answer often changes what the visitor wants to book")
        # Search answers only the question half: the Scheduler offers real times in the same reply,
        # so Search must not improvise booking advice (invented links, "can't book" disclaimers)
        s1 = await self._call_agent(
            search.run, req.model_copy(update={"params": {**req.params, "booking_handled_separately": True}}),
            "search")
        # ...and the Scheduler sees ONLY the booking half. A note telling it to ignore the question
        # was not enough: shown the whole message, the model still answered it ("4 to 8 weeks").
        # So the question is removed from its input, rebuilt from the classifier's booking intent.
        sched = next((i for i in intents if i["intent"] in sched_intents), {})
        detail = ", ".join(str(v) for v in (sched.get("payload") or {}).values() if isinstance(v, str) and v)
        booking_msg = {"schedule": "I'd like to book a discovery call",
                       "reschedule": "I'd like to move my booked call",
                       "cancel": "I'd like to cancel my booked call"}[sched.get("intent", "schedule")]
        booking_msg += f" ({detail})." if detail else "."
        s2 = await self._call_agent(
            scheduler.run, req.model_copy(update={
                "message": booking_msg,
                "params": {**req.params, "question_handled_separately": True}}),
            "scheduler")
        decision["chosen_agents"] = ["search", "scheduler"]
        return {"decision": decision, "responses": [s1, s2],
                "slots": s2.output.get("slots", []) if s2.output else []}

    async def route_conflict(self, t: dict) -> dict:
        req, decision = t["req"], dict(t["decision"])
        sched_intents = [i["intent"] for i in t["intents"] if i["intent"] in {"schedule", "reschedule", "cancel"}]
        decision["multi_intent_policy"] = "clarify"
        decision["reason"] = f"conflicting scheduling intents {sched_intents}; asked which one"
        q = await complete_json(CLARIFY_SYS, f"Conflicting request: {req.message}", max_tokens=150,
                                name="orchestrator.clarify")
        decision["chosen_agents"] = ["orchestrator:clarify"]
        return {"decision": decision, "slots": [], "responses": [
            AgentResponse(agent=AGENT, output={"reply": q.get("question", "Which would you like first?")})]}

    async def route_parallel(self, t: dict) -> dict:
        """Two independent questions: retrieval is read-only, so both run at once."""
        req, decision = t["req"], dict(t["decision"])
        decision["multi_intent_policy"] = "parallel"
        decision["reason"] = "two independent questions; retrieval is read-only so they run in parallel"
        payloads = [i.get("payload", {}).get("question") or req.message
                    for i in t["intents"] if i["intent"] == "search"][:2]
        reqs = [req.model_copy(update={"message": p}) for p in payloads]
        results = await asyncio.gather(*(self._call_agent(search.run, r, "search") for r in reqs))
        decision["chosen_agents"] = ["search", "search"]
        return {"decision": decision, "responses": list(results), "slots": []}

    async def route_search(self, t: dict) -> dict:
        req, decision = t["req"], dict(t["decision"])
        decision["chosen_agents"] = ["search"]
        decision["reason"] = "single question about the company"
        r = await self._call_agent(search.run, req, "search")
        # FR-4.7 escalation: a low-confidence answer offers the founder instead of guessing
        if r.confidence is not None and r.confidence < settings.CONFIDENCE_FLOOR and r.output:
            r.output["reply"] = r.output.get("reply", "") + (
                "\n\nIf you need something more precise than that, a quick call with Baskaran is "
                "the fastest way - shall I look at times?")
            decision["reason"] += f"; low confidence {r.confidence} -> offered escalation"
        return {"decision": decision, "responses": [r], "slots": []}

    async def route_scheduler(self, t: dict) -> dict:
        req, decision = t["req"], dict(t["decision"])
        sched = next(i["intent"] for i in t["intents"] if i["intent"] in {"schedule", "reschedule", "cancel"})
        decision["chosen_agents"] = ["scheduler"]
        decision["reason"] = f"scheduling intent: {sched}"
        r = await self._call_agent(scheduler.run, req, "scheduler")
        return {"decision": decision, "responses": [r],
                "slots": r.output.get("slots", []) if r.output else []}

    # ---- after routing

    async def step_compose(self, t: dict) -> dict:
        """Sub-agent failures come back as data (FR-8.3); never a silent empty reply. Compose the draft."""
        responses, decision = t["responses"], dict(t["decision"])
        agent_errors = []
        for r in responses:
            if r.error:
                agent_errors.append(r.error.model_dump())
            if r.status == "error" and not r.reply:
                r.output = {**(r.output or {}), "reply": AGENT_FALLBACKS.get(r.agent, GENERIC_FALLBACK)}
        if agent_errors:
            decision["agent_errors"] = agent_errors
        draft = "\n\n".join(r.reply for r in responses if r.reply).strip() or \
            "Could you tell me a little more about what you're looking for?"
        # the widget shows plain text: "[Join](https://meet...)" would render with the markup
        # showing and the URL buried, so keep the address and drop the brackets
        draft = _MD_LINK.sub(r"\2", draft)
        # everything the draft may legitimately state: retrieved passages, calendar tool output
        # (slots, booking results) and source names - the outbound guardrail judges against this
        context = "\n\n".join(
            [f for r in responses for f in (r.output or {}).get("verified_facts", [])]
            + [f"source: {c}" for r in responses for c in r.citations]
        )
        return {"responses": responses, "decision": decision, "draft": draft, "context": context}

    async def step_guardrail(self, t: dict) -> dict:
        """Outbound guardrail, owned here only (FR-3.8, FR-7.1)."""
        req, responses, decision = t["req"], t["responses"], dict(t["decision"])
        draft, slots = t["draft"], t["slots"]
        outbound = await guardrail.check_outbound(req, draft, context=t["context"])
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
                    kept.append(_booking_confirmation(booked_now(r)) if booked_now(r)
                                else (v.safe_fallback or outbound.safe_fallback))
            draft = "\n\n".join(kept)
            if not slots_ok:
                slots = []
            decision["guardrail_partial"] = True
        elif outbound.verdict == "block":
            booked = next((booked_now(r) for r in responses if booked_now(r)), None)
            # the calendar created the event: never tell the visitor it isn't booked
            draft = _booking_confirmation(booked) if booked else (outbound.safe_fallback or draft)
            slots = []
        return {"draft": draft, "slots": slots, "outbound": outbound, "decision": decision}

    async def step_persist(self, t: dict) -> dict:
        """Merge state (only the Orchestrator writes), log the routing decision (FR-3.9), close if done."""
        state, responses, decision = t["state"], t["responses"], t["decision"]
        signals, outbound, trace_id = t["signals"], t["outbound"], t["trace_id"]
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

        await store.append_message(state.id, "assistant", t["draft"],
                                   agent=",".join(decision["chosen_agents"]) or "orchestrator")
        try:
            state = await store.update_with_retry(state.id, build_patch)
        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=trace_id, session_id=state.id, agent=AGENT,
                            payload={"detail": f"state update failed: {exc}"})

        await log_event("routing_decision", trace_id=trace_id, session_id=state.id, agent=AGENT,
                        payload={**decision,
                                 "route": t.get("route"),
                                 "engine": settings.ORCHESTRATOR_ENGINE,
                                 "graph_path": (t["path"] + ["persist"]) if t.get("path") else None,
                                 "intents": t["intents"],
                                 "classifier_reasoning": t["cls"].get("reasoning"),
                                 "guardrail_outbound": outbound.verdict,
                                 "guardrail_category": outbound.category,
                                 "confidences": [r.confidence for r in responses]},
                        latency_ms=self._elapsed_ms(t))

        if decision.get("end_conversation") and not state.summary_sent:    # FR-3.4
            await self._finalize_locked(state.id, complete=True, trace_id=trace_id)   # lease already held

        return {"state": state, "result": {"session_id": state.id, "reply": t["draft"], "trace_id": trace_id,
                                           "slots": t["slots"], "blocked": outbound.verdict == "block"}}

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
        booked = bool((state.booking or {}).get("event_id"))
        if not complete and not booked and not _worth_reporting(state):
            # An idle session with nothing a salesperson could act on (someone typed a line and left).
            # Mailing it would bury real leads; close it quietly. A deliberate end always reports.
            await log_event("lifecycle", trace_id=trace_id, session_id=session_id, agent=AGENT,
                            payload={"event": "idle_timeout_not_reported",
                                     "reason": "no contactable or substantive signal captured",
                                     "turns": len(state.history)})
            try:
                await store.update_with_retry(session_id, lambda fresh: {"status": "abandoned"})
            except Exception as exc:  # noqa: BLE001
                await log_event("error", trace_id=trace_id, session_id=session_id, agent=AGENT,
                                payload={"detail": f"abandon status update failed: {exc}"})
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
