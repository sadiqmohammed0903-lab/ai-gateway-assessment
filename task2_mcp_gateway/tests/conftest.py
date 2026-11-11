from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest_asyncio
from fastapi import FastAPI

from task2_mcp_gateway.app import Settings, create_app
from task2_mcp_gateway.auth import mint_token
from task2_mcp_gateway.mock_downstream import create_mock_downstream

SECRET = "unit-test-secret-that-is-long-enough-for-hs256"


@dataclass
class Env:
    client: httpx.AsyncClient
    downstream: FastAPI

    @property
    def calls(self) -> list[dict]:
        return self.downstream.state.calls


def headers_for(role: str, secret: str = SECRET, ttl: int = 3600) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token(role, secret, ttl)}"}


def rpc(method: str, id_: int | None = 1, params: object = None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def tool_call(name: object, id_: int = 1) -> dict:
    return rpc("tools/call", id_, {"name": name, "arguments": {}})


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    downstream = create_mock_downstream()
    transport = httpx.ASGITransport(app=downstream)
    gateway = create_app(
        Settings(jwt_secret=SECRET, downstream_url="http://downstream/mcp"), transport
    )
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
        ) as client:
            yield Env(client, downstream)


async def make_gateway_client(
    transport: httpx.AsyncBaseTransport,
) -> tuple[httpx.AsyncClient, FastAPI]:
    gateway = create_app(
        Settings(jwt_secret=SECRET, downstream_url="http://downstream/mcp"), transport
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ), gateway
