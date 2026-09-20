"""Orchestrator: classify -> route -> tools -> guardrail -> state (FR-3.1 .. FR-3.9)."""
from __future__ import annotations
import asyncio, json
from datetime import datetime, timezone

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, SessionState
from app.agents import guardrail, lead_summary, scheduler, search
from app.llm import complete_json
from app.observability.logger import log_event, new_trace_id, timer
from app.state.store import store

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


class Orchestrator:

    async def handle(self, visitor_key: str, message: str, visitor_tz: str | None = None) -> dict:
        trace_id = new_trace_id()
        state = await store.get_or_create_by_visitor_key(visitor_key, visitor_tz)
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
                    max_tokens=600,
                )
            except Exception as exc:  # noqa: BLE001
                cls = {"intents": [{"intent": "search", "confidence": 0.5}],
                       "qualification_signals": {}, "reasoning": f"classifier failed: {exc}"}

            intents = [i for i in cls.get("intents", []) if i.get("intent")]
            intents.sort(key=lambda i: float(i.get("confidence", 0)), reverse=True)
            signals = {k: v for k, v in (cls.get("qualification_signals") or {}).items() if v}
            top_conf = float(intents[0].get("confidence", 0)) if intents else 0.0

            state_patch: dict = {}
            if signals:
                state_patch["qualification"] = {**(state.qualification or {}), **signals}

            # ---- 3. route ----
            decision, responses, slots = await self._route(req, intents, top_conf)

            # ---- 4. compose the draft ----
            draft = "\n\n".join(r.reply for r in responses if r.reply).strip() or \
                "Could you tell me a little more about what you're looking for?"
            # the outbound guardrail must see the retrieved chunk text, not just source names
            context = "\n\n".join(
                [r.output["context"] for r in responses if r.output and r.output.get("context")]
                + [c for r in responses for c in r.citations]
            )

            # ---- 5. outbound guardrail, owned here only (FR-3.8, FR-7.1) ----
            # Tell it what kind of turn this is. A decline and a booking confirmation both have no
            # retrieved context, and judging them by the groundedness rule blocks correct replies.
            if any(r.error or (r.output and (r.output.get("manual_followup") or r.output.get("queued")))
                   for r in responses):
                kind = "failure"
            # `declined` is only set when retrieval returned nothing at all. When it returns
            # weakly-related chunks - "refund policy" scoring against the pricing chunk, "office in
            # Dubai" against the markets chunk - the Search agent answers instead, sets
            # answered=False, and used to fall through to kind="answer". The groundedness rule was
            # then applied to a correct "we have not published that", it blocked, and the visitor got
            # FALLBACKS["unauthorised_commitment"] - pricing copy in reply to a refund question
            # (FR-4.4). Both ways of finding nothing are the same kind of turn.
            elif any(r.output and (r.output.get("declined") or r.output.get("answered") is False)
                     for r in responses):
                kind = "decline"
            elif context:
                kind = "answer"
            else:
                kind = "action"
            outbound = await guardrail.check_outbound(req, draft, context=context, kind=kind)
            if outbound.verdict == "block":
                draft = outbound.safe_fallback or draft
                slots = []

            # ---- 6. merge state ----
            for r in responses:
                if r.state_patch:
                    merged_q = {**state_patch.get("qualification", state.qualification or {}),
                                **(r.state_patch.get("qualification") or {})}
                    state_patch.update(r.state_patch)
                    if merged_q:
                        state_patch["qualification"] = merged_q
            runs = list(state.agents_run or [])
            runs.append({"agents": decision["chosen_agents"],
                         "at": datetime.now(timezone.utc).isoformat(),
                         "trace_id": trace_id})
            state_patch["agents_run"] = runs[-40:]

            await store.append_message(state.id, "assistant", draft,
                                       agent=",".join(decision["chosen_agents"]) or "orchestrator")
            try:
                state = await store.update_with_retry(state.id, state_patch, state.version)
            except Exception as exc:  # noqa: BLE001
                await log_event("error", trace_id=trace_id, session_id=state.id, agent=AGENT,
                                payload={"detail": f"state update failed: {exc}"})

            # ---- 7. log the routing decision (FR-3.9) ----
            await log_event("routing_decision", trace_id=trace_id, session_id=state.id, agent=AGENT,
                            payload={**decision,
                                     "intents": intents,
                                     "classifier_reasoning": cls.get("reasoning"),
                                     "guardrail_outbound": outbound.verdict,
                                     "guardrail_kind": kind,
                                     "guardrail_category": outbound.category,
                                     "confidences": [r.confidence for r in responses]},
                            latency_ms=turn["ms"])

            # ---- 8. end of conversation or confirmed booking? (FR-3.4, FR-6.4) ----
            new_booking = (state_patch.get("booking") or {}).get("event_id")
            if decision.get("end_conversation") or new_booking:
                await self.finalize(state.id, complete=True, trace_id=trace_id)

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
            q = await complete_json(CLARIFY_SYS, f"Ambiguous message: {req.message}", max_tokens=150)
            decision.update(chosen_agents=["orchestrator:clarify"],
                            reason=f"top intent confidence {top_conf:.2f} < "
                                   f"{settings.INTENT_CONFIDENCE_FLOOR}; asked a clarifying question")
            return decision, [AgentResponse(agent=AGENT, confidence=top_conf,
                                            output={"reply": q.get("question", "Could you say a bit more?")})], []

        if "end_conversation" in names and not actionable:
            r = await complete_json(CLOSING_SYS, f"Visitor said: {req.message}", max_tokens=150)
            decision.update(chosen_agents=["orchestrator:close"], reason="visitor ended the conversation",
                            end_conversation=True)
            return decision, [AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Thanks for stopping by!")})], []

        if not actionable:
            if "provide_info" in names:
                reply = ("Thanks - noted. Would you like me to answer anything else about CloseFuture, "
                         "or set up a short call with Baskaran?")
                decision.update(chosen_agents=["orchestrator:ack"], reason="visitor supplied details only")
                return decision, [AgentResponse(agent=AGENT, output={"reply": reply})], []
            r = await complete_json(SMALLTALK_SYS, f"Visitor said: {req.message}", max_tokens=200)
            decision.update(chosen_agents=["orchestrator:smalltalk"], reason="greeting or chit-chat")
            return decision, [AgentResponse(agent=AGENT, output={"reply": r.get("reply", "Hello!")})], []

        # ---- multi-intent policy (FR-3.5), justification recorded per case ----
        sched_intents = [n for n in actionable if n in {"schedule", "reschedule", "cancel"}]
        has_search = "search" in actionable

        if has_search and sched_intents:
            decision["multi_intent_policy"] = "sequence"
            decision["reason"] = ("question + booking: answered first, then offered times, because the "
                                  "answer often changes what the visitor wants to book")
            s1 = await search.run(req)
            s2 = await scheduler.run(req)
            decision["chosen_agents"] = ["search", "scheduler"]
            slots = s2.output.get("slots", []) if s2.output else []
            return decision, [s1, s2], slots

        if len(sched_intents) > 1:
            decision["multi_intent_policy"] = "clarify"
            decision["reason"] = f"conflicting scheduling intents {sched_intents}; asked which one"
            q = await complete_json(CLARIFY_SYS, f"Conflicting request: {req.message}", max_tokens=150)
            decision["chosen_agents"] = ["orchestrator:clarify"]
            return decision, [AgentResponse(agent=AGENT, output={"reply": q.get("question", "Which would you like first?")})], []

        if actionable.count("search") > 1:
            decision["multi_intent_policy"] = "parallel"
            decision["reason"] = "two independent questions; retrieval is read-only so they run in parallel"
            payloads = [i.get("payload", {}).get("question") or req.message
                        for i in intents if i["intent"] == "search"][:2]
            reqs = [req.model_copy(update={"message": p}) for p in payloads]
            results = await asyncio.gather(*(search.run(r) for r in reqs))
            decision["chosen_agents"] = ["search", "search"]
            return decision, list(results), []

        if has_search:
            decision["chosen_agents"] = ["search"]
            decision["reason"] = "single question about the company"
            r = await search.run(req)
            # FR-4.7 escalation: a low-confidence answer offers the founder instead of guessing
            if r.confidence is not None and r.confidence < settings.CONFIDENCE_FLOOR and r.output:
                r.output["reply"] = r.output.get("reply", "") + (
                    "\n\nIf you need something more precise than that, a quick call with Baskaran is "
                    "the fastest way - shall I look at times?")
                decision["reason"] += f"; low confidence {r.confidence} -> offered escalation"
            return decision, [r], []

        decision["chosen_agents"] = ["scheduler"]
        decision["reason"] = f"scheduling intent: {sched_intents[0]}"
        r = await scheduler.run(req)
        return decision, [r], (r.output.get("slots", []) if r.output else [])

    # ------------------------------------------------------------------ closing

    async def finalize(self, session_id: str, *, complete: bool, trace_id: str | None = None) -> None:
        """Trigger the Lead-Summary agent once for this session (FR-3.4, FR-3.7, FR-6.4)."""
        trace_id = trace_id or new_trace_id()
        state = await store.get(session_id)
        if state is None:
            return
        # FR-6.6: one summary per session. A later trigger only goes out (as an update) when it
        # carries something the earlier mail did not: a partial lead that is now complete, or a
        # booking made after the summary was sent.
        sent = state.summary_sent
        booked = bool((state.booking or {}).get("event_id"))
        if sent and not ((complete and not sent.get("complete")) or (booked and not sent.get("booked"))):
            return
        req = AgentRequest(session_id=session_id, trace_id=trace_id, state=state,
                           params={"complete": complete})
        res = await lead_summary.run(req)
        patch = dict(res.state_patch or {})
        patch["status"] = "completed" if complete else "abandoned"
        if state.summary_sent:            # immutable record stays; the update mail is already sent
            patch.pop("summary_sent", None)
        try:
            await store.update_with_retry(session_id, patch, state.version)
        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=trace_id, session_id=session_id, agent=AGENT,
                            payload={"detail": f"finalize state update failed: {exc}"})


orchestrator = Orchestrator()
