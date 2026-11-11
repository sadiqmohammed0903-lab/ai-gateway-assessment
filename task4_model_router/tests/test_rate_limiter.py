import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from task4_model_router.db import Database
from task4_model_router.rate_limiter import (
    RateLimitExceeded,
    RequestTooLarge,
    SlidingWindowLimiter,
)
from task4_model_router.tests.conftest import FakeClock

LIMIT = 50_000


@asynccontextmanager
async def limiter_env(
    path: Path, clock: FakeClock | None = None, limit: int = LIMIT
) -> AsyncIterator[tuple[SlidingWindowLimiter, Database, FakeClock]]:
    clock = clock or FakeClock()
    db = Database(str(path / "limiter.db"))
    await db.start()
    try:
        yield SlidingWindowLimiter(db, limit, 60.0, clock), db, clock
    finally:
        await db.close()


def count_rows(path: Path, tenant: str | None = None) -> int:
    conn = sqlite3.connect(path / "limiter.db")
    try:
        if tenant:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM token_events WHERE tenant_id=?", (tenant,)
                ).fetchone()[0]
            )
        return int(conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0])
    finally:
        conn.close()


async def test_database_uses_wal_and_busy_timeout(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (_, db, _clock):
        async with db.connection() as conn:
            mode = (await (await conn.execute("PRAGMA journal_mode")).fetchone())[0]  # type: ignore[index]
            timeout = (await (await conn.execute("PRAGMA busy_timeout")).fetchone())[0]  # type: ignore[index]
    assert mode == "wal" and timeout == 5000
    assert (tmp_path / "limiter.db").exists()


async def test_under_the_limit_passes_and_at_the_limit_is_allowed(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        await limiter.reserve("t", 20_000, "r1")
        await limiter.reserve("t", 30_000, "r2")  # exactly 50,000
        assert await limiter.usage("t") == LIMIT
        with pytest.raises(RateLimitExceeded):
            await limiter.reserve("t", 1, "r3")


async def test_request_larger_than_the_whole_budget_is_not_retryable(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        with pytest.raises(RequestTooLarge):
            await limiter.reserve("t", LIMIT + 1, "r1")
    assert count_rows(tmp_path) == 0


async def test_retry_after_is_when_enough_tokens_free_up(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, clock):
        await limiter.reserve("t", 30_000, "a")  # t=1000, expires at 1060
        clock.advance(30)
        await limiter.reserve("t", 20_000, "b")  # t=1030, expires at 1090
        clock.advance(10)  # t=1040, used=50,000

        with pytest.raises(RateLimitExceeded) as small:
            await limiter.reserve("t", 5_000, "c")  # freeing "a" (30k) is enough -> 1060
        assert small.value.retry_after == 20

        with pytest.raises(RateLimitExceeded) as big:
            await limiter.reserve("t", 40_000, "d")  # needs "a" and "b" gone -> 1090
        assert big.value.retry_after == 50


async def test_window_slides_capacity_returns_and_old_rows_are_evicted(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, clock):
        await limiter.reserve("t", LIMIT, "full")
        clock.advance(59)
        with pytest.raises(RateLimitExceeded) as exc:
            await limiter.reserve("t", 1_000, "early")
        assert exc.value.retry_after == 1
        assert count_rows(tmp_path, "t") == 1

        clock.advance(2)  # 61s after the first event
        await limiter.reserve("t", 1_000, "after")
        assert await limiter.usage("t") == 1_000
    assert count_rows(tmp_path, "t") == 1, "expired row should have been deleted"


async def test_event_exactly_at_window_edge_is_expired(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, clock):
        await limiter.reserve("t", LIMIT, "edge")
        clock.advance(60)
        await limiter.reserve("t", LIMIT, "next")


async def test_settle_releases_unused_tokens_and_release_returns_everything(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        a = await limiter.reserve("t", 10_000, "a")
        b = await limiter.reserve("t", 10_000, "b")
        assert await limiter.usage("t") == 20_000
        await limiter.settle(a, 1_234)
        assert await limiter.usage("t") == 11_234
        await limiter.release(b)
        assert await limiter.usage("t") == 1_234


async def test_tenants_are_isolated(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        await limiter.reserve("a", LIMIT, "a1")
        with pytest.raises(RateLimitExceeded):
            await limiter.reserve("a", 1, "a2")
        await limiter.reserve("b", LIMIT, "b1")
        assert await limiter.usage("b") == LIMIT


async def test_fifty_concurrent_reservations_never_exceed_the_limit(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        results = await asyncio.gather(
            *(limiter.reserve("t", 2_000, f"r{i}") for i in range(50)), return_exceptions=True
        )
        admitted = [r for r in results if not isinstance(r, BaseException)]
        rejected = [r for r in results if isinstance(r, RateLimitExceeded)]
        assert len(admitted) == 25 and len(rejected) == 25
        assert await limiter.usage("t") == LIMIT


async def test_concurrent_reservations_across_two_tenants(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _clock):
        coros = [limiter.reserve(t, 7_000, f"{t}{i}") for i in range(10) for t in ("a", "b")]
        results = await asyncio.gather(*coros, return_exceptions=True)
        assert sum(not isinstance(r, BaseException) for r in results) == 14  # 7 per tenant
        assert await limiter.usage("a") == 49_000 and await limiter.usage("b") == 49_000


async def test_evict_expired_clears_idle_tenants(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, clock):
        await limiter.reserve("idle", 100, "i1")
        await limiter.reserve("active", 100, "a1")
        clock.advance(30)
        assert await limiter.evict_expired() == 0
        clock.advance(31)
        assert await limiter.evict_expired() == 2
    assert count_rows(tmp_path) == 0


async def test_state_survives_reopening_the_database(tmp_path: Path) -> None:
    clock = FakeClock()
    async with limiter_env(tmp_path, clock) as (limiter, _, _c):
        await limiter.reserve("t", 40_000, "r1")
    async with limiter_env(tmp_path, clock) as (limiter, _, _c):
        assert await limiter.usage("t") == 40_000
        with pytest.raises(RateLimitExceeded):
            await limiter.reserve("t", 20_000, "r2")


async def test_duplicate_request_id_fails_without_corrupting_the_window(tmp_path: Path) -> None:
    async with limiter_env(tmp_path) as (limiter, _, _c):
        await limiter.reserve("t", 1_000, "same")
        with pytest.raises(sqlite3.IntegrityError):
            await limiter.reserve("t", 1_000, "same")
        assert await limiter.usage("t") == 1_000
        await limiter.reserve("t", 1_000, "other")  # connection is usable (rolled back)
