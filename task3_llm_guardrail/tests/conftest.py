import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI

from task3_llm_guardrail.app import Settings, create_app

URL = "http://upstream/v1/chat/completions"
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}


def sse_chunk(content: str | None, finish: str | None = None, role: str | None = None) -> bytes:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload = {"id": "c1", "object": "chat.completion.chunk", "model": "m", "created": 1}
    payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    return f"data: {json.dumps(payload)}\n\n".encode()


def parse_sse(text: str) -> list[Any]:
    """Return decoded `data:` payloads (JSON objects, or the string [DONE])."""
    events: list[Any] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        assert block.startswith("data: "), block
        data = block[len("data: ") :]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


def content_of(events: list[Any]) -> str:
    return "".join(
        c["delta"].get("content") or ""
        for e in events
        if isinstance(e, dict)
        for c in e.get("choices", [])
    )


class LazyStream(httpx.AsyncByteStream):
    """Upstream body that is produced on demand and records whether it was closed early."""

    def __init__(self, producer: AsyncIterator[bytes]) -> None:
        self._producer = producer
        self.closed = False
        self.produced = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for piece in self._producer:
            self.produced += 1
            yield piece

    async def aclose(self) -> None:
        self.closed = True
        await self._producer.aclose()  # type: ignore[attr-defined]


def lazy_transport(stream: LazyStream, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=stream)

    return httpx.MockTransport(handler)


@asynccontextmanager
async def gateway_client(transport: httpx.AsyncBaseTransport) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(upstream_url=URL), transport)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            yield client


@asynccontextmanager
async def running_app(transport: httpx.AsyncBaseTransport) -> AsyncIterator[FastAPI]:
    app = create_app(Settings(upstream_url=URL), transport)
    async with app.router.lifespan_context(app):
        yield app


async def call_asgi(
    app: FastAPI,
    body: dict[str, Any],
    disconnect_after: int | None = None,
    on_body: Callable[[bytes], None] | None = None,
) -> int:
    """Drive the ASGI app directly (no buffering) and optionally disconnect mid-stream.

    Returns the number of non-empty body messages the client received.
    """
    disconnected = asyncio.Event()
    request_sent = False
    received = 0
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
        "state": {},
    }

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal received
        if message["type"] == "http.response.body" and message.get("body"):
            received += 1
            if on_body is not None:
                on_body(message["body"])
            if disconnect_after is not None and received >= disconnect_after:
                disconnected.set()

    await app(scope, receive, send)  # type: ignore[arg-type]
    return received
