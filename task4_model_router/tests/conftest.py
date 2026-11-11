import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from task4_model_router.app import Settings, create_app
from task4_model_router.mock_providers import create_mock_provider
from task4_model_router.tokens import TokenCounter

KEY_A, KEY_B = "key-a", "key-b"


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CharCounter(TokenCounter):
    """One token per character: makes reservation sizes exact and predictable in tests."""

    def count(self, text: str) -> int:
        return len(text)


@dataclass
class Env:
    client: httpx.AsyncClient
    app: FastAPI
    primary: FastAPI
    secondary: FastAPI
    clock: FakeClock
    db_path: Path

    async def post(
        self,
        prompt: str = "hello",
        max_tokens: int = 100,
        key: str | None = KEY_A,
        **extra: Any,
    ) -> httpx.Response:
        headers = {"X-API-Key": key} if key else {}
        body = {"prompt": prompt, "max_tokens": max_tokens, **extra}
        return await self.client.post("/v1/completions", json=body, headers=headers)

    def rows(self, tenant: str | None = None) -> list[tuple[str, int, str]]:
        """(tenant_id, tokens, status) rows straight from the SQLite file."""
        conn = sqlite3.connect(self.db_path)
        try:
            query = "SELECT tenant_id, tokens, status FROM token_events"
            params: tuple[str, ...] = ()
            if tenant:
                query += " WHERE tenant_id = ?"
                params = (tenant,)
            return [(r[0], r[1], r[2]) for r in conn.execute(query + " ORDER BY id", params)]
        finally:
            conn.close()


@asynccontextmanager
async def make_env(
    tmp_path: Path,
    *,
    primary_mode: str = "normal",
    secondary_mode: str = "normal",
    clock: FakeClock | None = None,
    raise_app_exceptions: bool = True,
    primary_transport: httpx.AsyncBaseTransport | None = None,
    db_name: str = "gateway.db",
    **settings: Any,
) -> AsyncIterator[Env]:
    clock = clock or FakeClock()
    primary = create_mock_provider("primary", primary_mode)
    secondary = create_mock_provider("secondary", secondary_mode)
    db_path = tmp_path / db_name
    cfg = Settings(
        database_path=str(db_path),
        primary_url="http://primary/v1/completions",
        secondary_url="http://secondary/v1/completions",
        seed_tenants=(("tenant-a", KEY_A), ("tenant-b", KEY_B)),
        **settings,
    )
    app = create_app(
        cfg,
        primary_transport or httpx.ASGITransport(app=primary),
        httpx.ASGITransport(app=secondary),
        clock,
        CharCounter(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://gateway",
        ) as client:
            yield Env(client, app, primary, secondary, clock, db_path)
