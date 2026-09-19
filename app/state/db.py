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


class Pool:
    def __init__(self, pool: ThreadedConnectionPool, size: int) -> None:
        self._pool = pool
        self._slots = asyncio.Semaphore(size)

    @asynccontextmanager
    async def acquire(self):
        await self._slots.acquire()
        raw = None
        try:
            raw = await asyncio.to_thread(self._pool.getconn)
            raw.autocommit = True
            yield Connection(raw)
        finally:
            if raw is not None:
                await asyncio.to_thread(self._pool.putconn, raw)
            self._slots.release()

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.closeall)


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 10) -> Pool:
    pool = await asyncio.to_thread(ThreadedConnectionPool, min_size, max_size, dsn)
    return Pool(pool, max_size)
