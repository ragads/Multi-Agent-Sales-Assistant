"""Session store. Single writer + optimistic locking + advisory locks (FR-2.1 .. FR-2.6)."""
from __future__ import annotations
import json, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings
from app.contracts import SessionState
from app.state import db


class StaleVersionError(RuntimeError):
    pass


class Store:
    def __init__(self) -> None:
        self.pool: Optional[db.Pool] = None

    async def connect(self, dsn: str | None = None) -> None:
        """Defaults to the least-privilege runtime role. Only ingest passes the admin DSN."""
        if self.pool is None:
            self.pool = await db.create_pool(dsn or settings.SUPABASE_DB_URL, min_size=1, max_size=10)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    # ---------------- sessions ----------------

    async def get_or_create_by_visitor_key(self, visitor_key: str, visitor_tz: str | None = None) -> SessionState:
        """FR-2.3 / FR-2.5: a returning visitor resumes; only a genuinely new key starts fresh."""
        async with self.pool.acquire(visitor_key=visitor_key) as con:
            row = await con.fetchrow(
                """
                select * from sessions
                 where visitor_key = $1 and status in ('active', 'booked', 'abandoned', 'completed')
                   and expires_at > now()
                 order by last_activity_at desc limit 1
                """,
                visitor_key,
            )
            if row is None:
                row = await con.fetchrow(
                    """
                    insert into sessions (visitor_key, visitor_tz, expires_at)
                    values ($1, $2, now() + ($3 || ' days')::interval)
                    returning *
                    """,
                    visitor_key, visitor_tz, str(settings.SESSION_EXPIRY_DAYS),
                )
            elif row["status"] in ("abandoned", "completed"):
                # FR-2.3: a returning visitor resumes; the idle sweeper watches the session again
                row = await con.fetchrow(
                    "update sessions set status = 'active', visitor_tz = coalesce(visitor_tz, $2) "
                    "where id = $1 returning *", row["id"], visitor_tz)
            elif visitor_tz and not row["visitor_tz"]:
                row = await con.fetchrow(
                    "update sessions set visitor_tz = $2 where id = $1 returning *", row["id"], visitor_tz
                )
            # The session is resolved now, so narrow the RLS scope to it before touching messages.
            await con.set_scope(visitor_key=visitor_key, session_id=row["id"])
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def get(self, session_id: str) -> Optional[SessionState]:
        async with self.pool.acquire(session_id=session_id) as con:
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
            history=history,
        )

    async def append_message(self, session_id: str, role: str, content: str,
                             agent: str | None = None, metadata: dict | None = None) -> None:
        """Append-only: concurrent turns can never clobber each other (FR-2.6)."""
        async with self.pool.acquire(session_id=session_id) as con:
            await con.execute(
                "insert into messages (session_id, role, content, agent, metadata) values ($1,$2,$3,$4,$5)",
                uuid.UUID(session_id), role, content, agent, json.dumps(metadata or {}),
            )

    async def update(self, session_id: str, patch: dict[str, Any], expected_version: int) -> SessionState:
        """Optimistic lock. Raises StaleVersionError so the caller can re-read and re-apply (FR-2.6)."""
        allowed = {"status", "visitor_tz", "qualification", "agents_run", "booking", "summary_sent"}
        patch = {k: v for k, v in patch.items() if k in allowed}

        async with self.pool.acquire(session_id=session_id) as con:
            current = await con.fetchrow("select summary_sent from sessions where id=$1", uuid.UUID(session_id))
            # FR-6.7: summary_sent is immutable once written
            if current and current["summary_sent"] is not None and "summary_sent" in patch:
                patch.pop("summary_sent")

            sets, args = [], [uuid.UUID(session_id), expected_version]
            for i, (k, v) in enumerate(patch.items(), start=3):
                if k in {"qualification", "agents_run", "booking", "summary_sent"}:
                    sets.append(f"{k} = ${i}::jsonb")
                    args.append(json.dumps(v))
                else:
                    sets.append(f"{k} = ${i}")
                    args.append(v)
            sets.append("version = version + 1")
            sets.append("last_activity_at = now()")

            row = await con.fetchrow(
                f"update sessions set {', '.join(sets)} where id = $1 and version = $2 returning *", *args
            )
            if row is None:
                raise StaleVersionError(f"session {session_id} changed under us (expected v{expected_version})")
            history = await self._history(con, row["id"])
        return self._to_state(row, history)

    async def update_with_retry(self, session_id: str, patch: dict[str, Any],
                                expected_version: int, attempts: int = 3) -> SessionState:
        for n in range(attempts):
            try:
                return await self.update(session_id, patch, expected_version)
            except StaleVersionError:
                fresh = await self.get(session_id)
                if fresh is None:
                    raise
                expected_version = fresh.version
                if n == attempts - 1:
                    raise
        raise StaleVersionError(session_id)

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        """Postgres advisory lock: two long tool calls for one session cannot interleave (FR-2.6)."""
        key = abs(hash(session_id)) % (2 ** 31)
        async with self.pool.acquire(session_id=session_id) as con:
            await con.execute("select pg_advisory_lock($1)", key)
            try:
                yield con
            finally:
                await con.execute("select pg_advisory_unlock($1)", key)

    # ---------------- sweeper helpers ----------------

    async def idle_sessions(self, minutes: int) -> list[str]:
        # No visitor is attached to a sweeper tick, so there is no request scope for the
        # session policies to match. idle_sessions() is a SECURITY DEFINER function that
        # returns ids and nothing else; the caller then re-enters the normal policy path
        # once per session. See sql/004_rls_policies.sql.
        async with self.pool.acquire() as con:
            rows = await con.fetch("select id from idle_sessions($1)", minutes)
        return [str(r["id"]) for r in rows]

    async def expire_sessions(self) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval("select expire_sessions()") or 0

    # ---------------- logs ----------------

    async def insert_log(self, event_type, trace_id, session_id, agent, payload, latency_ms) -> None:
        async with self.pool.acquire(session_id=session_id) as con:
            await con.execute(
                """insert into logs (session_id, trace_id, event_type, agent, payload, latency_ms)
                   values ($1,$2,$3,$4,$5,$6)""",
                uuid.UUID(session_id) if session_id else None,
                uuid.UUID(trace_id), event_type, agent, json.dumps(payload, default=str), latency_ms,
            )

    async def trace(self, session_id: str) -> list[dict[str, Any]]:
        async with self.pool.acquire(session_id=session_id) as con:
            rows = await con.fetch(
                "select * from logs where session_id = $1 order by id", uuid.UUID(session_id)
            )
        return [dict(r) for r in rows]

    # ---------------- email outbox ----------------

    async def outbox_claim(self, session_id: str, payload: dict) -> tuple[str, bool]:
        """Returns (row_id, is_new). Unique on session_id => exactly-once (FR-6.6)."""
        async with self.pool.acquire(session_id=session_id) as con:
            row = await con.fetchrow("select * from email_outbox where session_id = $1", uuid.UUID(session_id))
            if row:
                await con.execute(
                    "update email_outbox set payload=$2, updated_at=now() where id=$1",
                    row["id"], json.dumps(payload, default=str),
                )
                return str(row["id"]), False
            row = await con.fetchrow(
                "insert into email_outbox (session_id, payload) values ($1,$2) returning id",
                uuid.UUID(session_id), json.dumps(payload, default=str),
            )
            return str(row["id"]), True

    async def outbox_mark(self, row_id: str, session_id: str, status: str,
                          provider_id: str | None = None, error: str | None = None) -> None:
        # session_id is required because the email_outbox policies are session-scoped: without
        # it the UPDATE matches no row and the send would be silently forgotten.
        async with self.pool.acquire(session_id=session_id) as con:
            await con.execute(
                """update email_outbox
                      set status=$2, provider_id=coalesce($3, provider_id),
                          last_error=$4, attempts=attempts+1, updated_at=now()
                    where id=$1""",
                uuid.UUID(row_id), status, provider_id, error,
            )

    async def outbox_pending(self) -> list[dict[str, Any]]:
        # Cross-session scan with no visitor attached - same reasoning as idle_sessions().
        # The retry predicate lives inside the function, not here.
        async with self.pool.acquire() as con:
            rows = await con.fetch("select * from outbox_pending()")
        return [dict(r) for r in rows]


store = Store()
