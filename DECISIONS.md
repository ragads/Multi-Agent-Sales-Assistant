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

(0.35 is calibrated for OpenAI `text-embedding-3-small`. The floor depends on the embedding model:
it is 0.60 for Gemini `gemini-embedding-2`, measured on this corpus - see decision 11.)

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

Four layers, each covering a different failure:

1. **A lease lock serialises turns within one session.** `store.session_lock()` claims the session row
   by writing `lock_owner` (a random id) and `lock_expires_at` (now + 60 s) with a conditional
   `update ... where lock_owner is null or lock_expires_at < now()`. The Orchestrator holds it for the
   whole turn, so a second message from the same visitor waits (up to 45 s) and then runs against the
   state the first turn left behind. The sweeper and the `/end` API take the same lease, so a lead
   summary can never fire in the middle of a turn.
   Why a lease rather than `pg_advisory_lock` (the original design): the advisory lock was keyed on
   Python's `hash()`, which differs per process, so it did not lock across workers; it held a pooled
   connection for the whole booking conversation, LLM calls included; session-level advisory locks do
   not survive Supabase's transaction pooler; and a crashed worker could leave it held. The lease uses
   the session id itself as the key, holds no connection, is renewed by a heartbeat every 20 s while a
   long turn runs, and simply expires if the worker dies.
2. **Optimistic locking with re-derived patches.** `update ... where id = $1 and version = $2`. On a
   version conflict, `update_with_retry()` re-reads fresh state and calls the caller's
   `build_patch(fresh)` again, so "add these qualification signals" and "append this `agents_run`
   entry" are merged onto what the other writer saved. The original version retried the same patch,
   computed from the stale copy, which could overwrite the other writer's changes; that is fixed.
3. **`messages` is append-only.** Nothing rewrites history, so no interleaving can lose a message.
4. **One live session per visitor.** A partial unique index on `sessions (visitor_key) where status in
   ('active','booked')` plus `insert ... on conflict do nothing` means two tabs sending their first
   message at the same moment converge on one session instead of creating two.

Expiry slides with activity: every state write sets `expires_at = greatest(expires_at, now() + 30
days)`, so a long or returning conversation never expires while it is still in use; only 30 days of
silence closes it.

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

`email_outbox.session_id` is UNIQUE, so a second trigger cannot insert a second row. Which email goes
out is the Lead-Summary agent's tool-call decision (decision 14): on the first trigger it calls
`send_lead_summary`; on a later one it compares the new summary with the one already sent and calls
`update_lead_summary` only if something actionable changed, otherwise nothing. Code guards around that
decision make a duplicate impossible whatever the model does: a second `send` is refused, and only one
email is allowed per trigger. If the model declines the very first send, the outbox row stays pending
and the drain delivers it, so a lead is never lost to a model decision. Once `sessions.summary_sent` is written, the store
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

**The provider is configurable.** Every client (chat, retrieval, ingest) is built from
`settings.llm_client_kwargs`, so any OpenAI-compatible API works by setting `LLM_BASE_URL` and
`LLM_API_KEY`. The deployment currently runs on **Google Gemini's free tier**
(`gemini-3.5-flash-lite` for chat, `gemini-embedding-2` at 1536 dimensions), because the OpenAI
account ran out of credit. That model was chosen by testing: it passed both strict-JSON mode and tool
calling, while the larger Flash models were overloaded (503) or returned empty JSON. Switching
providers has three consequences that are handled explicitly:

- **Re-embed the corpus.** Vectors from different embedding models aren't comparable, so
  `python -m app.rag.ingest` must run after every embedding-model change.
- **Recalibrate thresholds.** Gemini similarities run higher across the board (measured: on-topic
  questions 0.69-0.77, an unrelated weather question 0.52), so `MIN_SIMILARITY` and `CONFIDENCE_FLOOR`
  are 0.60 on Gemini instead of 0.35 / 0.45 on OpenAI (see decisions 2 and 3).
- **Echo provider fields back in the tool loop.** Gemini attaches a `thought_signature` to each tool
  call and rejects the next request without it, so `run_with_tools()` returns tool calls exactly as
  received instead of rebuilding them.

Trade-offs of the free tier: lower rate limits (a burst of turns can hit 429s, which the retry policy
absorbs with backoff), slower turns, and Google may use free-tier prompts to improve its models - fine
for a demo, not for real visitor data in production.

## 12. Fail-loud configuration

`config.py` validates every environment variable at import time and exits with a readable message. A
half-configured deployment that silently mocks a calendar is exactly the prototype behaviour the FRD
rules out.

## 13. Agent framework: hand-built orchestrator, sub-agents and tool loop (no LangGraph or AutoGen)

The multi-agent system is built directly on the OpenAI SDK and plain Python rather than on an agent
framework such as LangGraph or AutoGen. All three pieces a framework would normally provide are our own
code:

- **Orchestrator** - `app/agents/orchestrator.py` is an ordinary class. Each turn runs the same fixed
  pipeline: inbound guardrail -> classify -> route -> compose -> outbound guardrail -> merge state ->
  log. Routing is explicit code in `_route()`, including the multi-intent policy (sequence, parallel
  via `asyncio.gather`, or clarify) from decision 5.
- **Sub-agents** - Search, Scheduler, Lead-Summary and Guardrail are plain async functions that take an
  `AgentRequest` and return an `AgentResponse` (`app/contracts.py`). The Pydantic contract is the whole
  interface between agents; there are no framework node or edge types.
- **Tool loop** - `run_with_tools()` in `app/llm.py` is a ~50-line OpenAI tool-calling loop with a
  `max_turns` cap. The Scheduler (calendar tools, discovered from the calendar MCP server) and the
  Lead-Summary agent (email tools) both run on it. Each agent passes its own `call_tool` callback,
  which is the policy boundary described in decision 14.

Why not LangGraph or AutoGen (AutoGen models agents as participants in a free-form group chat, which
suits open-ended collaboration but not a fixed, auditable routing policy; the points below apply to
both):

- **The control flow is small and mostly fixed.** A turn is one routing decision followed by at most
  two sub-agent calls, not a long-running graph with cycles. A graph DSL would add a layer of
  abstraction without adding anything we need.
- **The FRD requires tight control over exactly the parts a framework would own.** State lives in
  Supabase with optimistic locking and only the Orchestrator writes it (decision 6). The guardrail runs
  at exactly two checkpoints (decision 10). Errors have the exact FR-8.3 shape, retries follow our own
  retryable/non-retryable split (decision 7), and every routing decision is logged with its reason
  (FR-3.9). LangGraph's checkpointer would be a second copy of session state that had to be kept in
  sync with the `sessions` table that the abandoned-session sweeper reads.
- **Traceability and debugging.** Every step is ordinary code that writes to our own `logs` table, so
  the full trace for a conversation is at `/session/{id}`, and a stack trace points at our code rather
  than framework internals. Model-level tracing comes from Langfuse (decision 17), which wraps the
  OpenAI client directly, so it needs no framework either.
- **Fewer dependencies.** `requirements.txt` stays small and avoids the LangChain dependency tree and
  its frequent API changes.

Trade-offs: we don't get built-in checkpoint/resume, human-in-the-loop interrupts, graph visualisation
or streaming helpers, and we had to write and test the tool loop ourselves (including the `max_turns`
guard against runaway tool calls). If the system grows to many agents with handoffs or cycles, this
should be revisited. Because each agent is already a function from `AgentRequest` to `AgentResponse`,
each one would map almost one-to-one onto a LangGraph node, so a later migration would not require a
rewrite.

## 14. Tool calling: the model chooses the action, code owns the arguments that matter

Tool calling means the model is shown tool schemas and decides which tool to call and with which
arguments; our code executes the call and feeds the result back, in a loop (`run_with_tools()`). Two
agents work this way:

- **Scheduler** - chooses between `propose_slots`, `check_availability`, `create_event`,
  `modify_event`, `cancel_event`, or asking the visitor a question.
- **Lead-Summary** - chooses between `send_lead_summary`, `update_lead_summary`, or sending nothing.

Search, Guardrail and the Orchestrator's classifier stay as single structured-JSON calls on purpose:
each always does the same one thing (retrieve then answer, check, classify), so there is no choice for
a model to make, and a fixed pipeline is cheaper, faster and easier to test.

Letting a model choose an action is not the same as trusting its arguments. The model's arguments are
untrusted input, so every tool call passes through the agent's `call_tool` function before it reaches an
MCP server:

| Argument | Who controls it | How |
|---|---|---|
| `visitor_tz` (propose_slots) | code | from the session; hidden from the model's schema |
| `count`, `days_ahead` | model, clamped | 2-3 slots, 1-14 days |
| `start_iso` (create/modify) | model picks, code checks | must match a slot the server returned from `propose_slots` (stored in `sessions.proposed_slots`) |
| `end_iso` / `new_end_iso` | code | taken from the matched slot; hidden from the model |
| `visitor_email` | model, checked | must be a valid address the visitor actually typed (or already on the session) |
| `visitor_name` | code first | the session's known name wins over the model's value |
| `event_id` (modify/cancel) | code | from `sessions.booking`; hidden from the model, so it cannot touch another booking |
| `idempotency_key` | code | `session_id:start_iso`; hidden from the model |
| lead `summary` payload | code | built by extraction + deterministic scoring; the model only chooses the action |

A rejected call is returned to the model as a structured error (`SLOT_NOT_OFFERED`, `EMAIL_NOT_GIVEN`,
`NO_BOOKING`, `ALREADY_SENT`, ...) that it can recover from, for example by asking for the email, and
is logged as `rejected_by_policy`. It never reaches the calendar or the mailbox. The calendar server
then re-checks the booking rules itself (decision 16), so even a caller that skipped this layer could
not book an invalid slot.

## 15. MCP servers require authentication

The MCP servers expose actions with real side effects: calendar writes and emails. Without auth,
anyone who can reach port 8931 or 8932 could list the tools and call them - book meetings on the
founder's calendar or send email from our domain.

- Each server requires `Authorization: Bearer <token>` on every request, enforced by ASGI middleware in
  front of FastMCP (`app/mcp_servers/auth.py`). A missing or wrong token gets a 401 before any MCP
  handling, so tool listing is protected as well as tool calls. The comparison is constant-time.
- There is one token per server (`MCP_CALENDAR_TOKEN`, `MCP_EMAIL_TOKEN`), so a leaked email token
  cannot book meetings. Config refuses to start with a token shorter than 32 characters.
- The servers bind to `127.0.0.1` by default; in docker-compose they have no published ports and are
  only reachable on the internal network.
- The client (`app/mcp_client.py`) sends the right token per server over the streamable-HTTP transport.

For a multi-tenant or public deployment, the next step is the MCP spec's OAuth 2.1 flow (short-lived,
scoped tokens issued by an authorization server) instead of static shared secrets. For one backend
talking to its own two tool servers, a strong per-server bearer token over a private network is the
proportionate choice.

## 16. Booking rules are enforced by the calendar server

`create_event` and `modify_event` validate every slot on the server, whatever the caller sends
(`app/booking_rules.py`):

- exactly `SLOT_MINUTES` (30) long -> otherwise `INVALID_DURATION`;
- Monday-Friday, starting and ending inside `BUSINESS_HOURS`, evaluated in the calendar owner's time
  zone -> otherwise `OUTSIDE_BUSINESS_HOURS`;
- in the future -> otherwise `SLOT_IN_PAST`; unparseable -> `MALFORMED_TIME`.

`propose_slots` uses the same `within_business_hours()` function, so every proposed slot is bookable.
`modify_event` now also checks availability (ignoring the booking being moved), which it previously
skipped. All of these are non-retryable: retrying an invalid slot would fail the same way. The
Scheduler returns them to the model, which proposes new slots. The rules are pure functions with unit
tests (`tests/test_unit.py`).

## 17. Observability: Langfuse for model calls, our own logs for the agent flow

Two complementary layers:

- **Our `logs` table** records the agent-level flow: routing decisions with reasons, guardrail
  verdicts, tool calls, retries, fallbacks, and now one `llm_call` row per model call with the call
  name (e.g. `search.answer`), model, prompt/completion tokens, cost in USD and latency. It is always
  on, lives next to the session data, and powers `/session/{id}`.
- **Langfuse** (optional, on when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set) records every
  model generation with its full prompt, response, token usage and cost, and adds dashboards for
  latency, cost per session and error rates. `app/llm.py` swaps in Langfuse's drop-in OpenAI client, so
  every agent is traced without code changes. Each call carries our `trace_id` (one Langfuse trace per
  visitor turn), our `session_id` (Langfuse groups all turns of a conversation), and a name. The ids
  come from a context variable the Orchestrator sets once per turn, so they follow calls into
  `asyncio.gather` without being threaded through every agent's signature.

Why Langfuse over LangSmith: it is open source and self-hostable (visitor conversations can stay on our
infrastructure), it has a free cloud tier, and its OpenAI integration needs no LangChain. Full prompts
go to Langfuse rather than into our own `logs` table, because prompts contain visitor messages and the
logs table is kept lean and PII-redacted.
