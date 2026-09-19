"""Small async facade over psycopg2 (Windows-friendly, prebuilt wheels).

Exposes the handful of calls the rest of the app uses - pool.acquire(), con.fetch / fetchrow /
fetchval / execute, con.transaction() - with $1, $2 placeholders. psycopg2 is blocking, so every
call runs in a worker thread and the event loop is never stalled.
"""
from __future__ import annotations
import asyncio, re
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

psycopg2.extras.register_uuid()

_PLACEHOLDER = re.compile(r"\$(\d+)")


def _prepare(sql: str, args: tuple) -> tuple[str, dict]:
    """Turn $n placeholders into named %(pn)s ones so a parameter may be reused or reordered."""
    sql = sql.replace("%", "%%")
    return _PLACEHOLDER.sub(lambda m: f"%(p{m.group(1)})s", sql), {f"p{i}": v for i, v in enumerate(args, 1)}


class Connection:
    def __init__(self, raw) -> None:
        self._raw = raw

    def _run(self, sql: str, args: tuple, mode: str):
        q, params = _prepare(sql, args)
        with self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            if mode == "execute":
                return cur.statusmessage
            if mode == "fetch":
                return cur.fetchall()
            if mode == "fetchrow":
                return cur.fetchone()
            row = cur.fetchone()
            return next(iter(row.values())) if row else None

    async def fetch(self, sql: str, *args):
        return await asyncio.to_thread(self._run, sql, args, "fetch")

    async def fetchrow(self, sql: str, *args):
        return await asyncio.to_thread(self._run, sql, args, "fetchrow")

    async def fetchval(self, sql: str, *args):
        return await asyncio.to_thread(self._run, sql, args, "fetchval")

    async def execute(self, sql: str, *args):
        return await asyncio.to_thread(self._run, sql, args, "execute")

    @asynccontextmanager
    async def transaction(self):
        await self.execute("begin")
        try:
            yield
        except BaseException:
            await self.execute("rollback")
            raise
        else:
            await self.execute("commit")

    async def set_scope(self, *, visitor_key: str | None = None, session_id=None) -> None:
        """Stamp this connection with the request it is serving, for the RLS policies to read.

        The policies in sql/004_rls_policies.sql filter every row against app.visitor_key and
        app.session_id, so a statement that forgets its WHERE clause returns nothing instead of
        another visitor's conversation. Connections run with autocommit on, which means a
        transaction-local set_config would be gone by the next statement - these are set at
        session level and cleared again in Pool.acquire()'s finally block.
        """
        await self.execute(_SET_SCOPE, visitor_key or "", str(session_id) if session_id else "")


_SET_SCOPE = (
    "select set_config('app.visitor_key', $1, false), set_config('app.session_id', $2, false)"
)


def _clear_scope(raw) -> None:
    with raw.cursor() as cur:
        cur.execute(
            "select set_config('app.visitor_key', '', false), "
            "       set_config('app.session_id', '', false)"
        )


class Pool:
    def __init__(self, pool: ThreadedConnectionPool, size: int) -> None:
        self._pool = pool
        self._slots = asyncio.Semaphore(size)

    @asynccontextmanager
    async def acquire(self, *, visitor_key: str | None = None, session_id=None):
        await self._slots.acquire()
        raw = None
        try:
            raw = await asyncio.to_thread(self._pool.getconn)
            raw.autocommit = True
            con = Connection(raw)
            await con.set_scope(visitor_key=visitor_key, session_id=session_id)
            yield con
        finally:
            if raw is not None:
                # A connection whose scope cannot be cleared must never be handed to the next
                # request carrying the previous visitor's scope - drop it instead.
                cleared = True
                try:
                    await asyncio.to_thread(_clear_scope, raw)
                except Exception:
                    cleared = False
                await asyncio.to_thread(self._pool.putconn, raw, None, not cleared)
            self._slots.release()

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.closeall)


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 10) -> Pool:
    pool = await asyncio.to_thread(ThreadedConnectionPool, min_size, max_size, dsn)
    return Pool(pool, max_size)
