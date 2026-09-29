-- Session hardening: lease lock, offered slots, one live session per visitor.
-- Run after 001 and 002 (Supabase SQL editor, or psql "$SUPABASE_DB_URL" -f sql/003_session_hardening.sql)

-- Slots returned by propose_slots, so create_event can only book a slot the server actually offered.
alter table sessions add column if not exists proposed_slots jsonb not null default '[]'::jsonb;

-- Lease lock (replaces the connection-bound pg_advisory_lock). A turn claims the session by writing
-- its own owner id with an expiry; a crashed worker's lease simply runs out.
alter table sessions add column if not exists lock_owner uuid;
alter table sessions add column if not exists lock_expires_at timestamptz;

-- Close out time-expired sessions and older duplicates before adding the unique index below.
update sessions set status = 'expired'
 where status in ('active', 'booked') and expires_at <= now();

update sessions s set status = 'expired'
 where s.status in ('active', 'booked')
   and exists (select 1 from sessions t
                where t.visitor_key = s.visitor_key
                  and t.status in ('active', 'booked')
                  and (t.last_activity_at, t.id) > (s.last_activity_at, s.id));

-- Two tabs sending their first message at the same moment must land in the same session.
create unique index if not exists sessions_one_live_per_visitor
  on sessions (visitor_key) where status in ('active', 'booked');
