# Design decisions and their justification

The FRD asks for several choices to be *justified*, not merely made. Each is recorded here with the
reasoning, so a reviewer can see why the number is what it is.

## 1. Chunk size 700 characters, overlap 120 (FR-1.3)

The source is a company profile made of short, already-topical sections. Two failure modes bracket the
choice:

- Chunks much larger than ~200 tokens mix unrelated facts. A single chunk carrying both the pricing
  table and the start of a case study produces an embedding that sits between the two topics and
  retrieves poorly for either.
- Chunks much smaller than ~100 tokens lose the subject of the sentence. "It launched in July 2026"
  is useless without knowing that "it" is Webiz.

700 characters is roughly 180 tokens, which holds one section-sized idea. The 120-character overlap
(~30 tokens) carries the antecedent across a split so the second half of a section still knows what it
is about. The chunker splits on markdown headings first and never breaks a line, which keeps bullets
and table rows intact. Result on the current corpus: 14 documents, 49 chunks, longest 692 characters.

## 2. Retrieval: top-k 6, minimum similarity 0.35 (FR-4.3, FR-4.4)

k=6 comfortably covers a question that spans two documents (e.g. "what do you use for payments?" hits
both the tech stack and two case studies) without stuffing the context with noise. The 0.35 cosine
floor is what separates "weakly related" from "unrelated" on this corpus; below it the Search agent
declines rather than guesses. If the top hits sit within 0.05 of each other but come from different
source documents, all of them are kept - that is the multi-document case in FR-4.6.

## 3. Confidence formula (FR-4.7)

`confidence = 0.6 * top_similarity + 0.4 * self_rated_groundedness`

Retrieval similarity alone is a poor proxy: a chunk can be topically close yet not contain the answer.
The model's own groundedness rating alone is optimistic. Weighting retrieval slightly higher keeps the
signal anchored to something measurable. Below 0.45 the Orchestrator appends an escalation offer rather
than presenting a shaky answer as fact.

## 4. Intent-confidence floor 0.60 (FR-3.6)

Below 0.60 on the top intent, the Orchestrator asks one clarifying question instead of routing.
Misrouting is more expensive than a single extra turn: sending a booking request to Search wastes the
visitor's time, and sending a question to the Scheduler makes the bot look pushy.

## 5. Multi-intent policy (FR-3.5)

Decided per case, and the choice is written into the routing log every time:

- **Question + booking -> sequence.** The answer often changes what the visitor wants to book, so
  Search runs first and the Scheduler then proposes times in the same reply.
- **Two independent questions -> parallel.** Retrieval is read-only and touches no shared state, so
  `asyncio.gather` is safe and halves latency.
- **Conflicting scheduling intents (e.g. book and cancel) -> clarify.** Guessing here mutates a real
  calendar, so the cost of being wrong is high and a question is cheap.

## 6. Concurrency strategy (FR-2.6)

Three mechanisms rather than one lock:

1. `messages` is append-only. A new visitor turn arriving while a tool call is in flight cannot
   overwrite conversation history, because nothing rewrites it.
2. `sessions` uses optimistic locking: `update ... where id = $1 and version = $2`. A losing writer
   re-reads fresh state, re-applies its patch and retries up to three times, so no write is silently
   dropped.
3. Long tool calls take a Postgres advisory lock keyed on the session id, so two bookings for one
   session cannot interleave.

Only the Orchestrator writes state, and only by applying a `state_patch`. Sub-agents read but never
write, which removes whole classes of race by construction.

## 7. Retry policy (FR-8.2)

3 attempts, exponential backoff 1s / 2s / 4s with jitter. Retryable: timeouts, connection errors, 408,
429, 5xx. Non-retryable: 4xx, invalid credentials, malformed requests and `SLOT_TAKEN`. Retrying a
non-retryable failure wastes the visitor's time and, for 429, can deepen a rate limit. `SLOT_TAKEN` is
deliberately non-retryable: the correct response is to propose new slots, not to try the same slot
again.

## 8. Lead score rubric (FR-6.5)

Scoring is deterministic code, not an LLM judgement, so the same conversation always produces the same
score and sales can trust the ordering of their inbox. Weights reflect buying signal strength: a booked
meeting (25) and a captured email (20) dominate, because they are the two things that let sales act at
all. Tiers: 70+ hot, 40-69 warm, below 40 cold.

## 9. Exactly-once email (FR-6.6, FR-6.7)

`email_outbox.session_id` is UNIQUE, so a second trigger cannot insert a second row. The first trigger
sends; a later trigger calls `update_lead_summary`, which is explicitly framed as an update to the
earlier mail rather than a conflicting duplicate. Once `sessions.summary_sent` is written, the store
layer refuses any patch that would overwrite it - the record is immutable, which is what lets the
Orchestrator know a summary already went out.

## 10. Guardrail placement (FR-3.8, FR-7.1)

The Guardrail is a standalone module with two entry points, called by the Orchestrator only - once
inbound, once outbound. Duplicating checks inside each agent would mean four places to update when a
rule changes, and inconsistent enforcement the moment one of them drifts. Outbound fails *closed*: if
the classifier is unavailable we substitute a safe fallback rather than sending unverified text.
Inbound fails *open*, because blocking every visitor during a classifier outage is worse than letting
an ordinary question through - the outbound check still protects what we say back.

## 11. Single-provider LLM choice (OpenAI, gpt-4o-mini)

The agent's reasoning and its embeddings both run on OpenAI, behind one API key. Two reasons:

- **Operational simplicity.** One provider, one key, one place to watch for rate limits or an outage,
  which matters for a system that already has five agents and two MCP servers to reason about.
- **Cost shape fits the workload.** Every agent turn is several small, structured calls (intent
  classification, a grounded answer, an inbound guardrail check, an outbound guardrail check) rather
  than one long generation. gpt-4o-mini is priced for exactly that pattern and is more than capable of
  strict-JSON extraction and tool-calling at this scope, so a larger model would add cost without
  adding correctness here.
- `app/llm.py` is intentionally the only file that talks to the model provider. `complete_json()` and
  `run_with_tools()` are the entire surface every agent depends on, so swapping providers later - or
  using a bigger model for one agent only - is a change to that one file, not five.

## 12. Fail-loud configuration

`config.py` validates every environment variable at import time and exits with a readable message. A
half-configured deployment that silently mocks a calendar is exactly the prototype behaviour the FRD
rules out.
