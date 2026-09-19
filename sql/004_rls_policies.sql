-- CloseFuture - row-level security policies for the application role.
-- Run after sql/003_app_role.sql.  Idempotent: safe to re-run.
--
-- ---------------------------------------------------------------------------
-- THE AUTHORIZATION MODEL
-- ---------------------------------------------------------------------------
-- This product has no end-user login. The widget is an anonymous chat embed on a
-- public marketing site, so auth.uid() is always NULL and there is nothing for an
-- auth.uid()-based policy to key on. The unit of ownership here is the *session*,
-- identified by the visitor_key the browser persists in localStorage (FR-2.5).
--
-- The backend therefore stamps each pooled connection with the scope of the request
-- it is serving, using two session GUCs set by app/state/db.py:
--
--     app.visitor_key  - the visitor this request belongs to
--     app.session_id   - the session this request is allowed to touch
--
-- Both are cleared when the connection returns to the pool, and a connection whose
-- scope cannot be cleared is discarded rather than reused. A policy that does not
-- match the current scope filters the row out, so a query that forgets its WHERE
-- clause returns nothing instead of another visitor's conversation.
--
-- ---------------------------------------------------------------------------
-- WHY NOT `FORCE ROW LEVEL SECURITY`
-- ---------------------------------------------------------------------------
-- FORCE makes RLS apply to the table owner too. That would break the SECURITY DEFINER
-- helpers below, the ingest job, and every future migration - and it is not a real
-- boundary, because an owner can simply ALTER TABLE ... NO FORCE. The boundary that
-- matters is that the application no longer connects as the owner.
--
-- ---------------------------------------------------------------------------
-- WHY SECURITY DEFINER FOR THREE FUNCTIONS
-- ---------------------------------------------------------------------------
-- Three background jobs legitimately need to look across sessions: the idle sweeper,
-- session expiry, and the email outbox drain. They run on a timer with no visitor
-- attached, so no request scope exists for them. Rather than widen the table policies
-- (which would make the per-session scoping decorative), each job gets one SECURITY
-- DEFINER function of fixed shape that returns only what that job needs. The app role
-- gets EXECUTE on those three functions and nothing else.

begin;

-- ===========================================================================
-- 1. Request scope helpers
-- ===========================================================================
-- STABLE, not IMMUTABLE: the value changes between statements on the same connection.
-- The session_id accessor shape-checks before casting so a malformed GUC filters the
-- row out instead of raising inside a policy.

create or replace function public.app_current_session_id() returns uuid
language sql stable
set search_path = pg_catalog, pg_temp
as $fn$
  select case
           when current_setting('app.session_id', true)
                ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
           then current_setting('app.session_id', true)::uuid
         end
$fn$;

create or replace function public.app_current_visitor_key() returns text
language sql stable
set search_path = pg_catalog, pg_temp
as $fn$
  select nullif(current_setting('app.visitor_key', true), '')
$fn$;

-- ===========================================================================
-- 2. Close the public API surface on these tables
-- ===========================================================================
-- `anon` and `authenticated` are reachable by anyone holding the publishable key, and
-- both were carrying SELECT/INSERT/UPDATE/DELETE/TRUNCATE on every table. Only the
-- empty policy set was keeping them out. None of these tables is meant to be reached
-- over PostgREST at all - the FastAPI backend is the only client - so the grants go.
-- `service_role` is revoked as well: the service key is being removed from the project,
-- and a key that is no longer issued should not still map to full table access.

revoke all on table
  public.sessions, public.messages, public.logs,
  public.email_outbox, public.documents, public.chunks
from anon, authenticated, service_role;

revoke all on table
  public.sessions, public.messages, public.logs,
  public.email_outbox, public.documents, public.chunks
from public;

-- ===========================================================================
-- 3. Least-privilege grants for the application role
-- ===========================================================================
-- RLS filters rows; GRANT decides which verbs exist at all. Both are needed: a verb
-- with no grant is refused before any policy runs.

grant usage on schema public to closefuture_app;

-- sessions: read, create and update. No DELETE - only expire_sessions() removes data.
grant select, insert, update on table public.sessions to closefuture_app;

-- messages: append-only transcript (FR-2.6). No UPDATE and no DELETE grant, so the
-- append-only invariant is enforced by the database rather than by convention.
grant select, insert on table public.messages to closefuture_app;

-- logs: append-only audit trail (FR-3.9, FR-8.5). Same reasoning.
grant select, insert on table public.logs to closefuture_app;

-- email_outbox: claimed, then marked sent/failed. Never deleted by the app.
grant select, insert, update on table public.email_outbox to closefuture_app;

-- chunks: read-only. The retrieval path reads; only ingest writes, and ingest runs as
-- the admin role. documents gets no grant at all - nothing at runtime reads it.
grant select on table public.chunks to closefuture_app;

-- bigserial columns need their sequence, or the INSERTs above fail.
grant usage, select on sequence public.messages_id_seq to closefuture_app;
grant usage, select on sequence public.logs_id_seq     to closefuture_app;

-- match_chunks needs `public` on its path: the `chunks` table and the pgvector `<=>`
-- operator both live there. 002_functions.sql created it without one.
alter function public.match_chunks(vector, int, text, float) set search_path = public, pg_temp;
grant execute on function public.match_chunks(vector, int, text, float) to closefuture_app;
grant execute on function public.app_current_session_id()  to closefuture_app;
grant execute on function public.app_current_visitor_key() to closefuture_app;

-- ===========================================================================
-- 4. Row-level security
-- ===========================================================================

alter table public.sessions      enable row level security;
alter table public.messages      enable row level security;
alter table public.logs          enable row level security;
alter table public.email_outbox  enable row level security;
alter table public.documents     enable row level security;
alter table public.chunks        enable row level security;

-- --------------------------------------------------------------- sessions --
-- Two ways in, because the app reaches a session by either identifier:
-- get_or_create_by_visitor_key() arrives with only the visitor_key, while every later
-- call has resolved the session id. Neither branch can reach a third party's row.

drop policy if exists "Application can view sessions for the current visitor" on public.sessions;
create policy "Application can view sessions for the current visitor"
  on public.sessions for select to closefuture_app
  using (
    visitor_key = public.app_current_visitor_key()
    or id = public.app_current_session_id()
  );

drop policy if exists "Application can create a session for the current visitor" on public.sessions;
create policy "Application can create a session for the current visitor"
  on public.sessions for insert to closefuture_app
  with check (visitor_key = public.app_current_visitor_key());

drop policy if exists "Application can update sessions for the current visitor" on public.sessions;
create policy "Application can update sessions for the current visitor"
  on public.sessions for update to closefuture_app
  using (
    visitor_key = public.app_current_visitor_key()
    or id = public.app_current_session_id()
  )
  with check (
    visitor_key = public.app_current_visitor_key()
    or id = public.app_current_session_id()
  );

-- --------------------------------------------------------------- messages --

drop policy if exists "Application can view messages in the current session" on public.messages;
create policy "Application can view messages in the current session"
  on public.messages for select to closefuture_app
  using (session_id = public.app_current_session_id());

drop policy if exists "Application can append messages to the current session" on public.messages;
create policy "Application can append messages to the current session"
  on public.messages for insert to closefuture_app
  with check (session_id = public.app_current_session_id());

-- ------------------------------------------------------------------- logs --
-- The NULL branch is deliberate and narrow: startup/shutdown lifecycle events and
-- API-level errors are raised before any session exists, and logger.log_event()
-- writes them with session_id NULL. Without it those writes fail and, because logging
-- swallows its own errors, they would fail silently. Reads stay session-scoped, so a
-- session trace can never surface another visitor's events.

drop policy if exists "Application can view logs for the current session" on public.logs;
create policy "Application can view logs for the current session"
  on public.logs for select to closefuture_app
  using (session_id = public.app_current_session_id());

drop policy if exists "Application can write logs for the current session" on public.logs;
create policy "Application can write logs for the current session"
  on public.logs for insert to closefuture_app
  with check (
    session_id = public.app_current_session_id()
    or session_id is null
  );

-- ----------------------------------------------------------- email_outbox --
-- One row per session (unique on session_id) gives exactly-once send semantics
-- (FR-6.6). Scoping on session_id means a request can only ever touch its own row.

drop policy if exists "Application can view the outbox row for the current session" on public.email_outbox;
create policy "Application can view the outbox row for the current session"
  on public.email_outbox for select to closefuture_app
  using (session_id = public.app_current_session_id());

drop policy if exists "Application can create the outbox row for the current session" on public.email_outbox;
create policy "Application can create the outbox row for the current session"
  on public.email_outbox for insert to closefuture_app
  with check (session_id = public.app_current_session_id());

drop policy if exists "Application can update the outbox row for the current session" on public.email_outbox;
create policy "Application can update the outbox row for the current session"
  on public.email_outbox for update to closefuture_app
  using (session_id = public.app_current_session_id())
  with check (session_id = public.app_current_session_id());

-- ----------------------------------------------------------------- chunks --
-- The one unconditional policy in this file, and the reason is specific: `chunks` holds
-- the public marketing corpus that is already published on closefuture.io. It contains
-- no visitor data, no PII and nothing session-scoped, and every single chat turn has to
-- search all of it - a scoped predicate would have nothing to scope on and would only
-- break retrieval. It is SELECT-only for one named role; the write path (ingest) runs
-- as the admin role and has no policy here.

drop policy if exists "Application can read the public knowledge base" on public.chunks;
create policy "Application can read the public knowledge base"
  on public.chunks for select to closefuture_app
  using (true);

-- -------------------------------------------------------------- documents --
-- Intentionally no policy and no grant. Nothing on the request path reads this table;
-- ingest is the only writer and it connects as the admin role. RLS stays enabled so the
-- table is deny-all to every non-owner role.

-- ===========================================================================
-- 5. Background jobs
-- ===========================================================================
-- Each of these runs on a timer with no visitor attached. They are SECURITY DEFINER so
-- they execute as the owner, and each one is deliberately shaped so it cannot be used
-- as a general-purpose read: the search_path is pinned, EXECUTE is granted only to the
-- application role, and the predicate is fixed in the function body.

-- Sweeper (FR-3.7). Returns ids only - never transcript or qualification data. The
-- caller then re-enters the normal policy path with app.session_id set per session.
create or replace function public.idle_sessions(idle_minutes int)
returns table (id uuid)
language sql
security definer
set search_path = public, pg_temp
stable as $fn$
  select s.id
    from public.sessions s
   where s.status = 'active'
     and s.summary_sent is null
     and s.last_activity_at < now() - make_interval(mins => greatest(idle_minutes, 1))
$fn$;

-- Session expiry (FR-2.4). Unchanged behaviour; now runs as definer and pins its
-- search_path. Only ever touches rows whose expires_at has already passed.
create or replace function public.expire_sessions()
returns int
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare n int;
begin
  delete from public.messages
   where session_id in (select id from public.sessions where expires_at < now());
  update public.sessions set status = 'expired'
   where expires_at < now() and status <> 'expired';
  get diagnostics n = row_count;
  return n;
end;
$fn$;

-- Outbox drain (FR-8.1). The retry predicate lives in the function rather than in the
-- caller, so this cannot be used to read outbox rows that are not due for a retry.
create or replace function public.outbox_pending()
returns table (id uuid, session_id uuid, payload jsonb, attempts int)
language sql
security definer
set search_path = public, pg_temp
stable as $fn$
  select o.id, o.session_id, o.payload, o.attempts
    from public.email_outbox o
   where o.status = 'pending'
     and o.attempts < 12
     and o.updated_at < now() - interval '2 minutes'
$fn$;

revoke all on function public.idle_sessions(int) from public, anon, authenticated, service_role;
revoke all on function public.expire_sessions()  from public, anon, authenticated, service_role;
revoke all on function public.outbox_pending()   from public, anon, authenticated, service_role;

grant execute on function public.idle_sessions(int) to closefuture_app;
grant execute on function public.expire_sessions()  to closefuture_app;
grant execute on function public.outbox_pending()   to closefuture_app;

commit;
