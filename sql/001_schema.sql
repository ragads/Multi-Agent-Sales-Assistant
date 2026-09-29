-- CloseFuture multi-agent chatbot - schema
-- Run this in the Supabase SQL editor (or: psql "$SUPABASE_DB_URL" -f sql/001_schema.sql)

create extension if not exists vector;
create extension if not exists pgcrypto;

-- ============ 1. Knowledge base (FR-1.4, FR-1.5, FR-1.6) ============
create table if not exists documents (
  id            uuid primary key default gen_random_uuid(),
  title         text not null unique,
  doc_type      text not null,
  source_ref    text not null,
  raw_content   text not null,
  created_at    timestamptz default now()
);

create table if not exists chunks (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid references documents(id) on delete cascade,
  chunk_index   int not null,
  content       text not null,
  category      text not null,
  source_ref    text not null,
  token_count   int,
  embedding     vector(1536),
  created_at    timestamptz default now()
);
create index if not exists chunks_embedding_idx
  on chunks using ivfflat (embedding vector_cosine_ops) with (lists = 100);
create index if not exists chunks_category_idx on chunks (category);

-- ============ 2. Session state (FR-2.1 .. FR-2.6) ============
create table if not exists sessions (
  id                uuid primary key default gen_random_uuid(),
  visitor_key       text not null,
  status            text not null default 'active',
  version           int  not null default 0,
  visitor_tz        text,
  qualification     jsonb not null default '{}'::jsonb,
  agents_run        jsonb not null default '[]'::jsonb,
  booking           jsonb,
  summary_sent      jsonb,
  last_activity_at  timestamptz not null default now(),
  expires_at        timestamptz not null default now() + interval '30 days',
  created_at        timestamptz default now()
);
create index if not exists sessions_visitor_key_idx on sessions (visitor_key);
create index if not exists sessions_status_idx on sessions (status, last_activity_at);

-- append-only: a concurrent turn can never overwrite history (FR-2.6)
create table if not exists messages (
  id          bigserial primary key,
  session_id  uuid references sessions(id) on delete cascade,
  role        text not null,
  content     text not null,
  agent       text,
  metadata    jsonb default '{}'::jsonb,
  created_at  timestamptz default now()
);
create index if not exists messages_session_idx on messages (session_id, id);

-- ============ 3. Observability (FR-3.9, FR-7.7, FR-8.5) ============
create table if not exists logs (
  id           bigserial primary key,
  session_id   uuid,
  trace_id     uuid not null,
  event_type   text not null,
  agent        text,
  payload      jsonb not null default '{}'::jsonb,
  latency_ms   int,
  created_at   timestamptz default now()
);
create index if not exists logs_session_idx on logs (session_id, created_at);
create index if not exists logs_trace_idx on logs (trace_id);

-- ============ 4. Email outbox (FR-6.6, FR-8.1) ============
create table if not exists email_outbox (
  id            uuid primary key default gen_random_uuid(),
  session_id    uuid unique,
  payload       jsonb not null,
  status        text not null default 'pending',
  attempts      int default 0,
  provider_id   text,
  last_error    text,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);
