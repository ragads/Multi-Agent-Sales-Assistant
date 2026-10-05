-- CloseFuture - security hardening (applied to the live project as two migrations:
--   security_hardening_devpulse_retention_role_limits, move_vector_extension_out_of_public).
-- Run as the admin role, after 003_app_role.sql and 004_rls_policies.sql.  Idempotent.

-- 1. A table in `public` without RLS is readable and writable by anyone holding the publishable
--    key. devpulse_users is not used by this app; deny-all until its owner adds policies.
alter table public.devpulse_users enable row level security;

-- 2. match_chunks is only for the application role, not the public API.
revoke execute on function public.match_chunks(extensions.vector, int, text, float) from public, anon, authenticated;
grant  execute on function public.match_chunks(extensions.vector, int, text, float) to closefuture_app;

-- 3. Cap what the runtime role can consume.
alter role closefuture_app connection limit 20;
alter role closefuture_app set statement_timeout = '30s';
alter role closefuture_app set idle_in_transaction_session_timeout = '60s';

-- 4. documents: explicit deny-all for the application role.
drop policy if exists "Nobody reads documents at runtime" on public.documents;
create policy "Nobody reads documents at runtime"
  on public.documents for select to closefuture_app using (false);

-- 5. Retention: an expired session loses its personal data, not just its messages.
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

  delete from public.logs
   where session_id in (select id from public.sessions where expires_at < now());

  update public.sessions
     set qualification = '{}'::jsonb,
         booking = case when booking is null then null else booking - 'attendee_email' end
   where expires_at < now()
     and (qualification <> '{}'::jsonb or (booking is not null and booking ? 'attendee_email'));

  update public.email_outbox
     set payload = jsonb_build_object('scrubbed', true,
                                      'lead_score', payload -> 'lead_score',
                                      'tier', payload -> 'tier')
   where status = 'sent'
     and payload ? 'visitor'
     and session_id in (select id from public.sessions where expires_at < now());

  update public.sessions set status = 'expired'
   where expires_at < now() and status <> 'expired';
  get diagnostics n = row_count;
  return n;
end;
$fn$;

revoke all on function public.expire_sessions() from public, anon, authenticated, service_role;
grant execute on function public.expire_sessions() to closefuture_app;

-- 6. pgvector out of `public` (Supabase linter 0014). The app role and the function need
--    `extensions` on their search path to resolve the vector type and the <=> operator.
create schema if not exists extensions;
alter extension vector set schema extensions;
grant usage on schema extensions to closefuture_app;
alter role closefuture_app set search_path = public, extensions;
alter function public.match_chunks(extensions.vector, int, text, float)
  set search_path = public, extensions, pg_temp;
