"""Session store. Single writer + optimistic locking + lease locks (FR-2.1 .. FR-2.6).

Row-level security: the application role (SUPABASE_DB_URL, e.g. closefuture_app) only sees rows of
the visitor/session it is serving. Every request-path query runs inside a transaction that first sets
`app.visitor_key` / `app.session_id` (transaction-local, so safe on pooled connections), which the RLS
policies read through app_current_visitor_key() / app_current_session_id(). Cross-session background
scans (idle sweep, expiry, outbox drain) and knowledge-base ingest use the separate admin pool
(SUPABASE_ADMIN_DB_URL), because by design the app role cannot see other visitors' rows.
"""
from __future__ import annotations
import asyncio, json, uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

import asyncpg

from app.config import settings
from app.contracts import SessionState


class StaleVersionError(RuntimeError):
    pass


class SessionBusyError(RuntimeError):
    """Another turn still holds this session's lease."""


JSONB_FIELDS = {"qualification", "agents_run", "booking", "summary_sent", "proposed_slots"}
LIVE = "status in ('active', 'booked') and expires_at > now()"


class Store:
    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None         # app role, RLS-scoped per visitor/session
        self.admin_pool: Optional[asyncpg.Pool] = None   # background scans + ingest only

    async def connect(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                settings.SUPABASE_DB_URL, min_size=1, max_size=10, statement_cache_size=0
            )
        if self.admin_pool is None:
            admin_url = settings.SUPABASE_ADMIN_DB_URL
            self.admin_pool = self.pool if not admin_url or admin_url == settings.SUPABASE_DB_URL else \
                await asyncpg.create_pool(admin_url, min_size=1, max_size=3, statement_cache_size=0)

    async def close(self) -> None:
        from app.observability.logger import flush_logs   # local import avoids a cycle
        await flush_logs()   # write any queued log rows before the pools go away
        if self.admin_pool and self.admin_pool is not self.pool:
            await self.admin_pool.close()
        self.admin_pool = None
        if self.pool:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def _scoped(self, *, session_id: Any = None, visitor_key: str | None = None):
        """A pooled connection with the RLS context set for one visitor/session.

        One round trip: both settings are written at connection level on every checkout, so a value
        left by the previous borrower is always overwritten (never inherited), and asyncpg's RESET ALL
        on release clears them as well. No explicit transaction, which saves a BEGIN and a COMMIT per
        call (~150 ms each to Supabase from here).
        """
        async with self.pool.acquire() as con:
            await con.execute(
                "select set_config('app.session_id', $1, false), set_config('app.visitor_key', $2, false)",
                str(session_id) if session_id else "", visitor_key or "")
            yield con

    @staticmethod
    async def _bind_session(con, session_id: Any) -> None:
        """Add the session id to the RLS context once a visitor-scoped lookup has found it."""
        await con.execute("select set_config('app.session_id', $1, false)", str(session_id))

    # ---------------- sessions ----------------

    async def get_or_create_by_visitor_key(self, visitor_key: str, visitor_tz: str | None = None) -> SessionState:
        """FR-2.3 / FR-2.5: a returning visitor resumes; only a genuinely new key starts fresh.

        Race-safe: the partial unique index sessions_one_live_per_visitor (sql/003) allows one live
        session per visitor_key, so two tabs sending a first message at once converge on one row.
        """
        async with self._scoped(visitor_key=visitor_key) as con:
            # a session past expires_at is closed here, not only by the 6-hourly sweep
            await con.execute(
                "update sessions set status = 'expired' "
                "where visitor_key = $1 and status in ('active', 'booked') and expires_at <= now()",
                visitor_key,
            )
            row = await con.fetchrow(f"select * from sessions where visitor_key = $1 and {LIVE}", visitor_key)
            if row is None:
                row = await con.fetchrow(
                    """
                    insert into sessions (visitor_key, visitor_tz, expires_at)
                    values ($1, $2, now() + ($3 || ' days')::interval)
                    on conflict (visitor_key) where status in ('active', 'booked') do nothing
                    returning *
                    """,
                    visitor_key, visitor_tz, str(settings.SESSION_EXPIRY_DAYS),
                )
                if row is None:   # the other tab won the insert; use its row
                    row = await con.fetchrow(f"select * from sessions where visitor_key = $1 and {LIVE}",
                                             visitor_key)
            elif visitor_tz and not row["visitor_tz"]:
                row = await con.fetchrow(
                    "update sessions set visitor_tz = $2 where id = $1 returning *", row["id"], visitor_tz
                )
            await self._bind_session(con, row["id"])
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def find_live_by_visitor_key(self, visitor_key: str) -> Optional[SessionState]:
        """Read-only lookup for the widget's history reload - never creates a session."""
        async with self._scoped(visitor_key=visitor_key) as con:
            row = await con.fetchrow(f"select * from sessions where visitor_key = $1 and {LIVE}", visitor_key)
            if row is None:
                return None
            await self._bind_session(con, row["id"])
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def get(self, session_id: str) -> Optional[SessionState]:
        async with self._scoped(session_id=session_id) as con:
            row = await con.fetchrow("select * from sessions where id = $1", uuid.UUID(session_id))
            if row is None:
                return None
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def _history(self, con, session_id) -> list[dict[str, Any]]:
        rows = await con.fetch(
            "select role, content, agent from messages where session_id = $1 order by id", session_id
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _to_state(row, history) -> SessionState:
        def j(v, default):
            if v is None:
                return default
            return json.loads(v) if isinstance(v, str) else v

        return SessionState(
            id=str(row["id"]),
            visitor_key=row["visitor_key"],
            status=row["status"],
            version=row["version"],
            visitor_tz=row["visitor_tz"],
            qualification=j(row["qualification"], {}),
            agents_run=j(row["agents_run"], []),
            booking=j(row["booking"], None),
            summary_sent=j(row["summary_sent"], None),
            proposed_slots=j(row["proposed_slots"], []),
            history=history,
        )

    async def append_message(self, session_id: str, role: str, content: str,
                             agent: str | None = None, metadata: dict | None = None) -> None:
        """Append-only: concurrent turns can never clobber each other (FR-2.6)."""
        async with self._scoped(session_id=session_id) as con:
            await con.execute(
                "insert into messages (session_id, role, content, agent, metadata) values ($1,$2,$3,$4,$5)",
                uuid.UUID(session_id), role, content, agent, json.dumps(metadata or {}),
            )

    async def update(self, session_id: str, patch: dict[str, Any], expected_version: int) -> SessionState:
        """Optimistic lock. Raises StaleVersionError so the caller can re-read and re-apply (FR-2.6)."""
        allowed = {"status", "visitor_tz"} | JSONB_FIELDS
        patch = {k: v for k, v in patch.items() if k in allowed}

        async with self._scoped(session_id=session_id) as con:
            current = await con.fetchrow("select summary_sent from sessions where id=$1", uuid.UUID(session_id))
            # FR-6.7: summary_sent is immutable once written
            if current and current["summary_sent"] is not None and "summary_sent" in patch:
                patch.pop("summary_sent")

            sets, args = [], [uuid.UUID(session_id), expected_version]
            for i, (k, v) in enumerate(patch.items(), start=3):
                if k in JSONB_FIELDS:
                    sets.append(f"{k} = ${i}::jsonb")
                    args.append(json.dumps(v))
                else:
                    sets.append(f"{k} = ${i}")
                    args.append(v)
            sets.append("version = version + 1")
            sets.append("last_activity_at = now()")
            # sliding expiry: a conversation that is still going never expires mid-flow (FR-2.4)
            args.append(str(settings.SESSION_EXPIRY_DAYS))
            sets.append(f"expires_at = greatest(expires_at, now() + (${len(args)} || ' days')::interval)")

            row = await con.fetchrow(
                f"update sessions set {', '.join(sets)} where id = $1 and version = $2 returning *", *args
            )
            if row is None:
                raise StaleVersionError(f"session {session_id} changed under us (expected v{expected_version})")
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def update_with_retry(self, session_id: str,
                                build_patch: Callable[[SessionState], dict[str, Any]],
                                attempts: int = 3) -> SessionState:
        """Optimistic write that re-derives the patch from FRESH state on every attempt.

        `build_patch(fresh_state) -> patch` runs after each re-read, so merges such as "add these
        qualification signals" or "append this agents_run entry" land on top of whatever a competing
        writer saved, instead of re-sending a patch computed from a stale copy.
        """
        for n in range(attempts):
            fresh = await self.get(session_id)
            if fresh is None:
                raise StaleVersionError(f"session {session_id} not found")
            try:
                return await self.update(session_id, build_patch(fresh), fresh.version)
            except StaleVersionError:
                if n == attempts - 1:
                    raise
        raise StaleVersionError(session_id)

    @asynccontextmanager
    async def session_lock(self, session_id: str, *, wait_s: float | None = None):
        """Lease lock on one session: one turn at a time per session (FR-2.6).

        A row-level lease (lock_owner + lock_expires_at) rather than pg_advisory_lock:
        - it does not pin a pooled connection for the whole turn, LLM calls included;
        - it works behind Supabase's transaction pooler, where session-level advisory locks don't;
        - the key is the session id itself, identical in every worker process;
        - if a worker dies the lease expires; a heartbeat renews it while a long turn is running.
        Raises SessionBusyError if the lease cannot be taken within `wait_s` (0 = try once).
        """
        owner = uuid.uuid4()
        sid = uuid.UUID(session_id)
        ttl = float(settings.SESSION_LOCK_TTL_S)
        wait_s = settings.SESSION_LOCK_WAIT_S if wait_s is None else wait_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s

        while True:
            async with self._scoped(session_id=session_id) as con:
                got = await con.fetchval(
                    """update sessions
                          set lock_owner = $2, lock_expires_at = now() + make_interval(secs => $3)
                        where id = $1 and (lock_owner is null or lock_expires_at < now())
                    returning id""",
                    sid, owner, ttl,
                )
            if got:
                break
            if loop.time() >= deadline:
                raise SessionBusyError(session_id)
            await asyncio.sleep(0.25)

        async def heartbeat():
            while True:
                await asyncio.sleep(ttl / 3)
                async with self._scoped(session_id=session_id) as con:
                    await con.execute(
                        "update sessions set lock_expires_at = now() + make_interval(secs => $3) "
                        "where id = $1 and lock_owner = $2", sid, owner, ttl)

        renew = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            renew.cancel()
            async with self._scoped(session_id=session_id) as con:
                await con.execute(
                    "update sessions set lock_owner = null, lock_expires_at = null "
                    "where id = $1 and lock_owner = $2", sid, owner)

    # ---------------- sweeper helpers ----------------

    async def idle_sessions(self, minutes: int) -> list[str]:
        async with self.admin_pool.acquire() as con:   # scans every visitor's sessions
            rows = await con.fetch(
                """
                select id from sessions
                 where status in ('active', 'booked')
                   and summary_sent is null
                   and (lock_owner is null or lock_expires_at < now())
                   and last_activity_at < now() - ($1 || ' minutes')::interval
                """,
                str(minutes),
            )
        return [str(r["id"]) for r in rows]

    async def expire_sessions(self) -> int:
        async with self.admin_pool.acquire() as con:
            return await con.fetchval("select expire_sessions()") or 0

    # ---------------- logs ----------------

    async def insert_log(self, event_type, trace_id, session_id, agent, payload, latency_ms) -> None:
        await self.insert_logs(session_id, [(event_type, trace_id, agent, payload, latency_ms)])

    async def insert_logs(self, session_id: str | None, rows: list[tuple]) -> None:
        """Batch insert of log rows for ONE session (the RLS policy scopes log writes by session)."""
        async with self._scoped(session_id=session_id) as con:
            await con.executemany(
                """insert into logs (session_id, trace_id, event_type, agent, payload, latency_ms)
                   values ($1,$2,$3,$4,$5,$6)""",
                [(uuid.UUID(session_id) if session_id else None, uuid.UUID(trace_id), event_type, agent,
                  json.dumps(payload, default=str), latency_ms)
                 for event_type, trace_id, agent, payload, latency_ms in rows],
            )

    async def trace(self, session_id: str) -> list[dict[str, Any]]:
        async with self._scoped(session_id=session_id) as con:
            rows = await con.fetch(
                "select * from logs where session_id = $1 order by id", uuid.UUID(session_id)
            )
        return [dict(r) for r in rows]

    # ---------------- email outbox ----------------

    async def outbox_claim(self, session_id: str, payload: dict) -> tuple[str, bool, Optional[dict], str]:
        """Returns (row_id, is_new, previous_payload, status). Unique on session_id => exactly-once (FR-6.6).

        A row that is still pending gets the newer payload, so the drain sends the latest summary.
        A row that was already sent keeps the payload that actually went out, so the Lead-Summary agent
        can compare old against new before deciding whether an update is worth sending.
        """
        async with self._scoped(session_id=session_id) as con:
            row = await con.fetchrow(
                """insert into email_outbox (session_id, payload) values ($1, $2)
                   on conflict (session_id) do nothing returning id""",
                uuid.UUID(session_id), json.dumps(payload, default=str),
            )
            if row:
                return str(row["id"]), True, None, "pending"
            row = await con.fetchrow("select * from email_outbox where session_id = $1", uuid.UUID(session_id))
            previous = row["payload"]
            previous = json.loads(previous) if isinstance(previous, str) else previous
            if row["status"] != "sent":   # pending, or failed and now given a fresh set of attempts
                await con.execute("update email_outbox set payload=$2, status='pending', attempts=0, "
                                  "updated_at=now() where id=$1",
                                  row["id"], json.dumps(payload, default=str))
                return str(row["id"]), False, previous, "pending"
            return str(row["id"]), False, previous, "sent"

    async def outbox_mark(self, row_id: str, status: str, provider_id: str | None = None,
                          error: str | None = None, payload: dict | None = None, *,
                          session_id: str | None = None) -> None:
        """Pass session_id on the request path (RLS-scoped); without it the admin pool is used (drain)."""
        ctx = self._scoped(session_id=session_id) if session_id else self.admin_pool.acquire()
        async with ctx as con:
            await con.execute(
                """update email_outbox
                      set status=$2, provider_id=coalesce($3, provider_id),
                          last_error=$4, attempts=attempts+1, updated_at=now(),
                          payload=coalesce($5::jsonb, payload)
                    where id=$1""",
                uuid.UUID(row_id), status, provider_id, error,
                json.dumps(payload, default=str) if payload is not None else None,
            )

    async def outbox_pending(self) -> list[dict[str, Any]]:
        async with self.admin_pool.acquire() as con:
            rows = await con.fetch(
                """select * from email_outbox
                    where status = 'pending' and attempts < 12
                      and updated_at < now() - interval '2 minutes'"""
            )
        return [dict(r) for r in rows]


store = Store()
