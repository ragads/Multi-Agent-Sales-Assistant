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

700 characters is roughly 180 tokens, which holds one section-sized idea. The chunker splits on
markdown headings first and never breaks a line, which keeps bullets and table rows intact.

**What this actually produces, measured rather than assumed.** The corpus is 14 documents containing
49 markdown sections, and the longest section is 692 characters. Every section therefore fits inside
one chunk: 49 sections in, 49 chunks out, one to one, and no chunk straddles a heading.

That is the intended outcome, and it has a consequence worth stating plainly rather than leaving for
a reader to discover: **the 120-character overlap never applies to this corpus.** Overlap exists to
carry the antecedent of a pronoun across a split, and nothing splits. Verified directly - zero of the
35 consecutive chunk pairs share any text. Writing the source documents in sections that each fit a
chunk is a better answer to FR-1.3 than relying on overlap to repair mid-section breaks, because a
chunk that begins at a heading never loses its subject in the first place.

The overlap setting is kept, not removed, because it is the guard for the case this corpus does not
currently hit. Add a section longer than 700 characters - a case study that grows, a new FAQ - and
`_pack()` splits it and the overlap starts doing its job. It is a bound on future content, and the
number is justified on that basis: 120 characters is ~30 tokens, enough to carry a sentence's subject
across a break without duplicating a meaningful share of a 180-token chunk.

## 2. Retrieval: top-k 6, minimum similarity 0.25 (FR-4.3, FR-4.4)

k=6 comfortably covers a question that spans two documents (e.g. "what do you use for payments?" hits
both the tech stack and two case studies) without stuffing the context with noise. If the top hits sit
within 0.05 of each other but come from different source documents, all of them are kept - that is the
multi-document case in FR-4.6.

The floor was 0.35, on the reasoning that it separated "weakly related" from "unrelated". Measured
against the corpus it did not. "How much does it cost" ranks the pricing chunk first at 0.268 and
"pricing" at 0.278 - the right chunk every time, both discarded, so the assistant answered "I don't
have that" on the most common question a sales visitor asks while the published rates sat in the
corpus. Only literal phrasing ("hourly rate", 0.485) cleared the bar, which is not how visitors write.

Nor can any floor make that call. A question that *should* be declined ("do you do blockchain
smart-contract audits") scores 0.318 - higher than the pricing question that should be answered.
Separating them requires reading the text, not comparing a cosine. That is the answer model's job and
it does it reliably: given retrieved chunks that do not address the question it says so plainly, which
is what FR-4.4 asks for and what it did on every decline tested.

So the floor now does the one job a distance metric is good at - excluding noise, which scores around
0.10 ("what is the weather in Paris", 0.097) - and the decision about relevance sits with the model
that can read. Verified after the change: pricing questions answer with the real published figures,
and the blockchain and weather questions still decline.

## 3. Confidence formula (FR-4.7)

`confidence = 0.6 * top_similarity + 0.4 * self_rated_groundedness`

Retrieval similarity alone is a poor proxy: a chunk can be topically close yet not contain the answer.
The model's own groundedness rating alone is optimistic. Weighting retrieval slightly higher keeps the
signal anchored to something measurable. Below 0.45 the Orchestrator appends an escalation offer rather
than presenting a shaky answer as fact.

## 4. Intent-confidence floor 0.72 (FR-3.6)

Below 0.72 on the top intent, the Orchestrator asks one clarifying question instead of routing.
Misrouting is more expensive than a single extra turn: sending a booking request to Search wastes the
visitor's time, and sending a question to the Scheduler makes the bot look pushy.

The floor started at 0.60, chosen on that reasoning alone. Measured, it never fired. The classifier is
not calibrated the way the number assumes - it rates a genuinely ambiguous message far higher than a
human would. "Can you help with the thing for my app?" is the FRD's own example of a message that
should be clarified rather than routed, and the classifier scored it above 0.60, so FR-3.6 was
answered with a guess in every test run. 0.72 sits above that score and below the 0.85-0.95 the
classifier gives messages that really are unambiguous.

This is a calibration knob, not a principle: it is tuned to the model named in `OPENAI_CHAT_MODEL`
and would need re-measuring against a different one. Lower it if the assistant starts over-clarifying.

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

## 6a. Moving a booking re-checks the slot, like making one (FR-5.5, FR-5.8)

FR-5.5 is written about `create_event`: re-check availability immediately before writing, because the
slot was proposed seconds ago and the gap between proposing and booking is exactly where a
double-booking comes from. `modify_event` had the same gap and none of the protection - it patched
the event straight to the new time.

The window is real in both directions. A visitor opens the reschedule link in their invite, the page
lists open times, they go and make a cup of tea, and meanwhile another visitor books one of those
times through the chat. On the agent path it is worse: the model supplies `new_start_iso`, so nothing
guaranteed the target was ever free. The result would be two calls stacked on the owner's calendar,
which is the precise outcome FR-5.5 exists to prevent - the fact that it arrived through a move
rather than a create makes no difference to the person whose calendar it is.

`modify_event` now takes `_BOOK_LOCK` and checks the target window before patching, returning the
same non-retryable `SLOT_TAKEN` a conflicting create returns. Sharing the one lock matters as much as
the check: a create and a move racing each other would otherwise both pass their own check and both
write.

The check uses `events().list` rather than the `freebusy()` query `create_event` uses, because
freebusy returns opaque busy blocks with no ids. An event shifted by fifteen minutes overlaps its own
current slot, so a freebusy check would find the event colliding with itself and refuse every small
adjustment. `events().list` returns ids, so the event being moved is excluded by id; events marked
"free" (`transparency: transparent`) and all-day entries are skipped too, since neither blocks a slot.

`SLOT_TAKEN` stays non-retryable here for the reason it is non-retryable on create: the fix is to
propose new times, not to try the same one again. The Scheduler already handles it that way, and the
self-service page surfaces it as a `409` with "that time was just taken - please pick another",
rather than the generic `503` it used to report for a collision that is recoverable in one click.

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

## 10a. The outbound Guardrail is told what kind of turn it is judging (FR-4.4, FR-7.4, FR-8.4)

The outbound check originally judged every draft by one rule set, the central rule being "block
anything not supported by the retrieved context chunks". That rule is correct for a retrieval-grounded
answer and actively wrong for every other kind of turn, because two of the replies the spec *requires*
have no context by construction:

- a **decline** has no chunks precisely because retrieval found none (FR-4.4), and
- a **failure notice** is emitted when a tool died before retrieval ever mattered (FR-8.4).

Both were being blocked and replaced with generic marketing copy, so the visitor was told neither that
the topic was unpublished nor that their booking had failed — strictly worse than the draft, and the
exact outcome those two requirements exist to prevent.

The Orchestrator now classifies each turn as `answer`, `decline`, `failure` or `action` and passes it
to `check_outbound`. Two things follow from that label:

1. **Required turns get their own prompt.** Appending "do not block this" to the permissive prompt did
   not work — the model had already been primed with the groundedness and commitment rules and simply
   changed which category it blocked under (`tone`, then `unauthorised_commitment`, then `leakage`,
   then `pii`). `decline` and `failure` turns are therefore judged by `OUTBOUND_REQUIRED_SYS`, which
   can only return `leakage` or `pii`. The categories that cannot logically apply are not on the menu.
2. **A blocked required turn keeps an honest substitute.** `REQUIRED_FALLBACKS` replaces a blocked
   decline with a plainer decline and a blocked failure notice with a plainer failure notice. This is
   the structural guarantee: however the classifier behaves, and even when it is unavailable and the
   check fails closed, the visitor still learns that something failed or that the topic is unpublished.
   Prompt wording reduces false blocks; this is what makes the requirement hold regardless.

Tone and groundedness are still enforced in full on `answer` turns, which is where they belong.

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

## 13. Database authorization: a least-privilege role, not the service key

The project originally carried `SUPABASE_SERVICE_KEY` in `.env` and connected to Postgres as
`postgres`. Two separate problems, one of which was invisible.

The key itself was never read by any code path - there is no `supabase` client in
`requirements.txt` and every query goes through psycopg2. It was pure liability: a credential that
grants unrestricted, RLS-bypassing access to every table, sitting in config and in the deploy
environment for no functional reason. It is gone.

The real issue was the connection. RLS was enabled on all six tables but had zero policies, which
reads as "locked down" and in fact was - for `anon` and `authenticated`. It was never enforced
against the application, because a table's owner is exempt from its own row-level security and the
app connected as the owner. The protection was accidental.

`auth.uid()` is not available as a scoping key here: the widget is an anonymous embed on a public
site and no visitor ever logs in. The unit of ownership is the session, keyed by the `visitor_key`
the browser persists (FR-2.5). So the app now connects as `closefuture_app` - no superuser, no
`BYPASSRLS`, owner of nothing - and `app/state/db.py` stamps each pooled connection with
`app.visitor_key` and `app.session_id` for the request it is serving, clearing them on release and
discarding any connection whose scope could not be cleared. The policies filter every row against
that scope, so a query that loses its `WHERE` clause returns nothing rather than someone else's
conversation.

Three things could not be expressed as a visitor-scoped policy, and each was handled rather than
waved through:

- **The sweeper, expiry and outbox drain** run on a timer with no visitor attached, and genuinely
  need to see across sessions. Widening the table policies to accommodate them would have made the
  scoping decorative. Instead each gets one `SECURITY DEFINER` function of fixed shape, with a
  pinned `search_path`, returning only what that job needs - `idle_sessions()` returns ids and
  nothing else - and `EXECUTE` granted to the app role alone.
- **Ingestion** rewrites `documents` and `chunks`. That is an operator task, not a request, so the
  runtime role is read-only on `chunks` and has no access at all to `documents`; ingest connects
  via `SUPABASE_ADMIN_DB_URL`. A compromised chat request cannot poison the knowledge base.
- **The knowledge base read** is the one `USING (true)` policy in the file. `chunks` holds the
  public marketing corpus already published on the website - no PII, nothing session-scoped - and
  every turn has to search all of it. There is nothing to scope on, and a predicate there would
  only break retrieval. It is `SELECT`-only, for one named role.

`FORCE ROW LEVEL SECURITY` was considered and rejected. It would apply RLS to the owner too, which
breaks the `SECURITY DEFINER` helpers, ingest and every future migration - and it is not a boundary
anyone is held by, since an owner can simply `ALTER TABLE ... NO FORCE`. The boundary that matters
is that the application no longer connects as the owner.

Two hardening steps came out of the same pass. `anon` and `authenticated` were carrying
`SELECT/INSERT/UPDATE/DELETE/TRUNCATE` on all six tables - the Supabase default for tables in
`public` - with only the empty policy set keeping them out. Those grants are revoked, as are
`service_role`'s: a key that is no longer issued should not still map to full table access. Nothing
reaches these tables over PostgREST; the FastAPI backend is the only client, and the browser widget
only ever talks to that backend.

## Abandoned sessions only report when there is a lead in them

FR-3.7 sends a partial lead summary for every session idle past
`SESSION_IDLE_TIMEOUT_MIN`. Taken literally that includes someone who opened the widget, typed one
line and closed the tab: a mail headed "Website visitor", no contact details, nothing to follow up.
Testing produced a steady stream of them, and at real traffic they would bury the leads that matter.

`finalize()` now reports an abandoned session only when it holds something actionable - an email,
company, project type, budget or timeline - or a booking. A name on its own does not qualify: it
gives nobody to contact and nothing to discuss. Sessions that fall short are still marked
`abandoned` and still logged, as `idle_timeout_not_reported` with the turn count, so the behaviour
is visible in the trace rather than silent.

A deliberate end (`complete=True`) always reports, whatever was captured, because the visitor chose
to finish rather than drifting off - and a booked session always reports.

## One signed link per purpose, not one per session

`sign_session()` originally signed the bare session id, and both links the system hands out were
built from that one value: the reschedule link in the visitor's calendar invite, and the transcript
link in the sales rep's lead email. They are the same string, so they open the same doors. Any
visitor who booked a call could take the `t` from their own invite, change `/booking/` to
`/session/`, and read the internal trace for their conversation - routing decisions and their
justifications, guardrail verdicts and categories, retrieved chunk text with similarity scores, and
their own lead score and qualification tier. That is the exact material `LEAK_PATTERNS` and the
`leakage` guardrail category exist to keep out of replies, reachable by editing one path segment.

The token is now bound to what it opens: `sign_session(session_id, purpose)` signs
`"<purpose>:<session_id>"`, and `require_session_access` takes the purpose it is guarding. A booking
token opens the booking page and nothing else. The admin token still opens everything, as before.

Invites already delivered carry the old undifferentiated token, so `_legacy_sign()` is accepted -
for `BOOKING` only, never for `TRACE`, which would reopen the leak. It is marked for deletion once
those bookings are in the past.

## Tool arguments are bound to session state, not to the prompt

The Scheduler's system prompt tells the model to take `event_id` from the session state it is given.
That is guidance, not authorization: `modify_event` and `cancel_event` move and delete real calendar
entries, and Google mails the attendee either way. A turn that talked the model into passing a
different id would have been honoured. `call_tool` now takes the id from `state.booking` and
discards whatever the model passed.

`create_event`'s `visitor_email` is handled the same way, with one honest limit. The address must
parse and must appear in the visitor's own messages or in `qualification.email` - both sources are
needed because the classifier's signals for the current turn are merged only after routing, so on
the turn where someone first gives their address it exists in the history and nowhere else. This
stops the model inventing or substituting an address. It does not stop a visitor from typing someone
else's and calling it theirs; that is bounded by the rate limits, not by this check.

## The lead email escapes everything it renders

`_render()` in the email MCP server interpolated name, company, budget, timeline, key questions and
next step straight into HTML. Every one of those comes out of the chat transcript, so a visitor who
typed markup got live markup in the mail landing in the sales inbox - a plausible link or an overlay
of the real transcript link, arriving from our own trusted sender. Every value is escaped now, and
`meet_link` and `conversation_url` render as links only when they are `https`, otherwise as inert
text. The session page in `app/main.py` already did this; the email had simply been missed.

## Signed links are logged without their query string

`hub.call` logs its full arguments, which is what makes the trace worth reading - but `manage_url`
and `conversation_url` carry their access token in `?t=`, and those rows go to the `logs` table and
to stdout, which on Render means the platform log viewer. `_redact` now strips the query string from
any `*_url` or `*_link` value, leaving the path visible and the token gone. An audit row should not
hold a working key to the thing it audits.
