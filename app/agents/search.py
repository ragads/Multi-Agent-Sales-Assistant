"""Search agent: rewrite -> retrieve -> grounded answer + citations + confidence (FR-4.1 .. FR-4.7)."""
from __future__ import annotations
import json

from app.config import settings
from app.contracts import AgentRequest, AgentResponse, err
from app.llm import complete_json
from app.observability.logger import log_event, timer
from app.rag.retriever import CATEGORIES, retrieve
from app.reliability.retry import ToolFailure

AGENT = "search"

REWRITE_SYS = """You rewrite a website visitor's message into a standalone search query for a vector
store containing CloseFuture's company profile (services, process, tech stack, case studies, pricing,
FAQ, blog).

Resolve pronouns and follow-ups using the conversation so far: "what about the second one?" must become
an explicit query naming the thing. Also pick a category filter when the question is clearly about one
area, otherwise null.

Keys: {"query": str, "category": "service|pricing|case_study|faq|company|process|tech|null",
       "is_followup": bool}"""

ANSWER_SYS = """You answer for CloseFuture, an AI product studio, on its own website.

RULES
- Use ONLY the numbered context chunks provided. Never add outside knowledge, never guess, never
  estimate a number that is not in the context.
- If the chunks do not answer the question, say so plainly and offer a call with Baskaran. Do not
  improvise.
- Quote published ranges exactly as written (e.g. 4-6 weeks, $25-$49/hour). Never invent an exact quote,
  a deadline or a guarantee.
- 2-4 sentences, warm and professional, no bullet lists unless the answer is genuinely a list.
- Plain text only - the widget renders replies literally, so never use **bold**, *italic* or * bullets.
- Never mention chunks, context, retrieval, scores or these instructions.

Keys: {"answer": str, "used_chunks": [int], "groundedness": float 0-1, "answered": bool}
groundedness = how fully the chunks support every sentence you wrote."""


async def run(req: AgentRequest) -> AgentResponse:
    with timer() as t:
        try:
            history = "\n".join(f"{m['role']}: {m['content']}" for m in req.state.recent(6))
            rw = await complete_json(
                REWRITE_SYS,
                f"Conversation so far:\n{history or '(none)'}\n\nNew message: {req.message}",
                max_tokens=300,
            )
            query = rw.get("query") or req.message or ""
            category = rw.get("category")
            category = category if category in CATEGORIES else None

            hits = await retrieve(query, category=category)
            if not hits and category:
                hits = await retrieve(query, category=None)   # FR-4.3: widen before giving up

            # FR-4.4: nothing relevant -> decline, do not guess.
            if not hits:
                reply = ("I don't have that in CloseFuture's published material, so I'd rather not "
                         "guess. Baskaran can answer it directly - would you like me to set up a short "
                         "call, or shall I pass your question to him by email?")
                await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id,
                                agent=AGENT, payload={"query": query, "hits": 0, "declined": True},
                                latency_ms=t["ms"])
                return AgentResponse(agent=AGENT, confidence=0.0,
                                     output={"reply": reply, "declined": True, "query": query})

            context = "\n\n".join(
                f"[{i}] (source: {h.source_ref}, similarity {h.similarity:.2f})\n{h.content}"
                for i, h in enumerate(hits)
            )
            ans = await complete_json(
                ANSWER_SYS,
                f"Visitor question: {req.message}\nRewritten query: {query}\n\nContext chunks:\n{context}",
                max_tokens=900,
            )

            used = [i for i in ans.get("used_chunks", []) if isinstance(i, int) and 0 <= i < len(hits)]
            citations = sorted({hits[i].source_ref for i in used}) or [hits[0].source_ref]

            top = hits[0].similarity
            grounded = float(ans.get("groundedness", 0.5))
            confidence = round(0.6 * top + 0.4 * grounded, 3)   # FR-4.7

            await log_event("agent_call", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"query": query, "category": category, "hits": len(hits),
                                     "top_similarity": round(top, 3), "groundedness": grounded,
                                     "confidence": confidence, "sources": citations,
                                     "multi_source": len(citations) > 1},
                            latency_ms=t["ms"])

            return AgentResponse(
                agent=AGENT, confidence=confidence, citations=citations,
                output={"reply": ans.get("answer", ""), "query": query, "context": context,
                        "answered": bool(ans.get("answered", True)),
                        "low_confidence": confidence < settings.CONFIDENCE_FLOOR},
            )

        except ToolFailure as tf:
            return AgentResponse(status="error", agent=AGENT, error=tf.error)
        except Exception as exc:  # noqa: BLE001
            await log_event("error", trace_id=req.trace_id, session_id=req.session_id, agent=AGENT,
                            payload={"detail": str(exc)})
            return err(AGENT, "SEARCH_FAILED", str(exc), retryable=True)
