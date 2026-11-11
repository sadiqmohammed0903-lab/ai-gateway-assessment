"""On-disk SQLite (WAL) with a small connection pool, tenants/API keys and token events."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash  TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id)
);
CREATE TABLE IF NOT EXISTS token_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    tokens     INTEGER NOT NULL,
    request_id TEXT    NOT NULL UNIQUE,
    status     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_events_tenant_ts ON token_events (tenant_id, ts);
"""


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class Database:
    """Pool of autocommit connections: callers manage transactions explicitly.

    SQLite allows one writer at a time across connections and processes; `BEGIN IMMEDIATE`
    plus `busy_timeout` makes concurrent writers queue instead of fail.
    """

    def __init__(self, path: str, pool_size: int = 4) -> None:
        self._path = path
        self._pool_size = pool_size
        self._idle: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all: list[aiosqlite.Connection] = []

    async def start(self) -> None:
        for i in range(self._pool_size):
            conn = await aiosqlite.connect(self._path, isolation_level=None)
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute("PRAGMA synchronous = NORMAL")
            if i == 0:
                await conn.execute("PRAGMA journal_mode = WAL")
                await conn.executescript(SCHEMA)
            self._all.append(conn)
            self._idle.put_nowait(conn)

    async def close(self) -> None:
        for conn in self._all:
            await conn.close()
        self._all.clear()
        self._idle = asyncio.Queue()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._idle.get()
        try:
            yield conn
        finally:
            self._idle.put_nowait(conn)

    async def seed_tenant(self, tenant_id: str, name: str, api_key: str) -> None:
        async with self.connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO tenants (id, name) VALUES (?, ?)", (tenant_id, name)
            )
            await conn.execute(
                "INSERT OR REPLACE INTO api_keys (key_hash, tenant_id) VALUES (?, ?)",
                (hash_key(api_key), tenant_id),
            )

    async def tenant_for_key(self, api_key: str) -> str | None:
        async with self.connection() as conn:
            cursor = await conn.execute(
                "SELECT tenant_id FROM api_keys WHERE key_hash = ?", (hash_key(api_key),)
            )
            row = await cursor.fetchone()
        return str(row[0]) if row else None
