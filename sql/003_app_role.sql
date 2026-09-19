-- CloseFuture - least-privilege database role for the running application.
--
-- WHY THIS FILE EXISTS
-- The app used to connect as `postgres`, the owner of every table in `public`.
-- A table owner is not subject to that table's row-level security, so although RLS was
-- enabled on all six tables, it was never actually enforced against the application.
-- This role is a plain, non-owner login role: NOSUPERUSER, NOBYPASSRLS, NOCREATEDB,
-- NOCREATEROLE. Every statement it runs is filtered by the policies in 004_rls_policies.sql.
--
-- RUN THIS FIRST, then 004_rls_policies.sql.
--
--   psql "$SUPABASE_ADMIN_DB_URL" \
--        -v app_password="$(openssl rand -base64 24)" \
--        -f sql/003_app_role.sql
--
-- In the Supabase SQL editor there is no :variable support - replace
-- :'app_password' with a quoted literal before running, and never commit the result.

\set ON_ERROR_STOP on

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'closefuture_app') then
    create role closefuture_app login
      nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
  end if;
end
$$;

alter role closefuture_app with password :'app_password';

-- Supabase's pooler authenticates as "<role>.<project_ref>"; the role itself stays unqualified.
-- Connection string for SUPABASE_DB_URL:
--   postgresql://closefuture_app.<project_ref>:<password>@<pooler_host>:5432/postgres
-- Port 5432 is the session-mode pooler, which the app requires: store.session_lock() holds a
-- pg_advisory_lock across statements and that needs a connection pinned for the whole block.
