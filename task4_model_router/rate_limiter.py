"""Token-aware sliding-window rate limiter backed by SQLite.

Algorithm: every admitted request writes a row (tenant, ts, tokens). The usage of a tenant is
the sum of `tokens` for rows with ts > now - window. Check-and-reserve is atomic: a per-tenant
asyncio.Lock serialises coroutines of this process, and `BEGIN IMMEDIATE` takes SQLite's write
lock so other connections or processes cannot interleave between the sum and the insert.

Eviction: rows with ts <= now - window never count toward the sum, so correctness does not
depend on deleting them. They are deleted (a) for the tenant on every reservation and (b) for
all tenants by `evict_expired`, which the app runs periodically, so idle tenants do not grow
the table forever.
"""

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite

from task4_model_router.db import Database

Clock = Callable[[], float]


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded, retry after {retry_after}s")
        self.retry_after = retry_after


class RequestTooLarge(Exception):
    """The request alone exceeds the whole window budget; retrying can never succeed."""


@dataclass(frozen=True)
class Reservation:
    request_id: str
    tenant_id: str
    tokens: int


async def _window_sum(conn: aiosqlite.Connection, tenant_id: str, cutoff: float) -> int:
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) FROM token_events WHERE tenant_id = ? AND ts > ?",
        (tenant_id, cutoff),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


class SlidingWindowLimiter:
    def __init__(
        self,
        db: Database,
        limit: int = 50_000,
        window_seconds: float = 60.0,
        clock: Clock = time.time,
    ) -> None:
        self._db = db
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def reserve(self, tenant_id: str, tokens: int, request_id: str) -> Reservation:
        if tokens > self.limit:
            raise RequestTooLarge
        async with self._locks[tenant_id], self._db.connection() as conn:
            now = self._clock()
            cutoff = now - self.window
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    "DELETE FROM token_events WHERE tenant_id = ? AND ts <= ?", (tenant_id, cutoff)
                )
                used = await _window_sum(conn, tenant_id, cutoff)
                if used + tokens > self.limit:
                    retry_after = await self._retry_after(conn, tenant_id, used, tokens, now)
                    await conn.execute("COMMIT")  # keeps the eviction; nothing else was written
                    raise RateLimitExceeded(retry_after)
                await conn.execute(
                    "INSERT INTO token_events (tenant_id, ts, tokens, request_id, status) "
                    "VALUES (?, ?, ?, ?, 'reserved')",
                    (tenant_id, now, tokens, request_id),
                )
                await conn.execute("COMMIT")
            except RateLimitExceeded:
                raise
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise
        return Reservation(request_id, tenant_id, tokens)

    async def _retry_after(
        self, conn: aiosqlite.Connection, tenant_id: str, used: int, tokens: int, now: float
    ) -> int:
        """Seconds until enough of the oldest events leave the window to fit `tokens`."""
        cursor = await conn.execute(
            "SELECT ts, tokens FROM token_events WHERE tenant_id = ? AND ts > ? ORDER BY ts ASC",
            (tenant_id, now - self.window),
        )
        remaining = used
        for ts, event_tokens in await cursor.fetchall():
            remaining -= event_tokens
            if remaining + tokens <= self.limit:
                return max(1, math.ceil(ts + self.window - now))
        return max(1, math.ceil(self.window))

    async def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace the reserved amount with the real usage (releases the unused part)."""
        await self._update(reservation, max(0, actual_tokens), "settled")

    async def release(self, reservation: Reservation) -> None:
        """Give the whole reservation back (the request failed)."""
        await self._update(reservation, 0, "released")

    async def _update(self, reservation: Reservation, tokens: int, status: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    "UPDATE token_events SET tokens = ?, status = ? WHERE request_id = ?",
                    (tokens, status, reservation.request_id),
                )
                await conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise

    async def usage(self, tenant_id: str) -> int:
        async with self._db.connection() as conn:
            return await _window_sum(conn, tenant_id, self._clock() - self.window)

    async def evict_expired(self) -> int:
        """Delete expired rows for every tenant. Returns the number of rows removed."""
        async with self._db.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "DELETE FROM token_events WHERE ts <= ?", (self._clock() - self.window,)
                )
                await conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise
            return cursor.rowcount

    async def cleanup_loop(self, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.evict_expired()
