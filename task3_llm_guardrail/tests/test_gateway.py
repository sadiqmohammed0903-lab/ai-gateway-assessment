import asyncio
import json
import logging
import tracemalloc
from collections.abc import AsyncIterator

import httpx
import pytest

from task3_llm_guardrail.mock_llm import DEFAULT_CHUNKS, create_mock_llm
from task3_llm_guardrail.redactor import redact_text
from task3_llm_guardrail.tests.conftest import (
    BODY,
    LazyStream,
    call_asgi,
    content_of,
    gateway_client,
    lazy_transport,
    parse_sse,
    running_app,
    sse_chunk,
)

RAW_PII = ["john.doe@example.com", "123-45-6789", "4111 1111 1111 1111"]


async def test_end_to_end_stream_is_valid_sse_and_redacted() -> None:
    upstream = httpx.ASGITransport(app=create_mock_llm())
    async with gateway_client(upstream) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.endswith("data: [DONE]\n\n")

    events = parse_sse(resp.text)
    assert events[-1] == "[DONE]" and events.count("[DONE]") == 1
    chunks = [e for e in events if e != "[DONE]"]
    assert all(
        c["object"] == "chat.completion.chunk" and c["id"] == "chatcmpl-mock" for c in chunks
    )

    text = content_of(events)
    assert text == redact_text("".join(DEFAULT_CHUNKS))
    assert text.count("[REDACTED]") == 3
    for raw in RAW_PII:
        assert raw not in resp.text
    assert "john" not in resp.text and "5-6789" not in resp.text


async def test_role_and_finish_reason_are_preserved() -> None:
    upstream = httpx.ASGITransport(app=create_mock_llm())
    async with gateway_client(upstream) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    chunks = [e for e in parse_sse(resp.text) if e != "[DONE]"]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_text_held_back_is_flushed_before_finish_reason() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield sse_chunk("contact me at bob@exam", role="assistant")
        yield sse_chunk("ple.com or call tom")
        yield sse_chunk(None, finish="stop")
        yield b"data: [DONE]\n\n"

    async with gateway_client(lazy_transport(LazyStream(producer()))) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    events = parse_sse(resp.text)
    chunks = [e for e in events if e != "[DONE]"]
    assert content_of(events) == "contact me at [REDACTED] or call tom"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"]["content"] == "tom"


async def test_upstream_ending_without_done_still_flushes_and_terminates() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield sse_chunk("write to bob@exam")
        yield sse_chunk("ple.com")

    async with gateway_client(lazy_transport(LazyStream(producer()))) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    events = parse_sse(resp.text)
    assert content_of(events) == "write to [REDACTED]"
    assert events[-1] == "[DONE]"


async def test_non_streaming_request_is_redacted_too() -> None:
    upstream = httpx.ASGITransport(app=create_mock_llm())
    async with gateway_client(upstream) as client:
        resp = await client.post("/v1/chat/completions", json=BODY | {"stream": False})
    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert content == redact_text("".join(DEFAULT_CHUNKS))


async def test_invalid_request_uses_standard_error_schema() -> None:
    async with gateway_client(httpx.ASGITransport(app=create_mock_llm())) as client:
        resp = await client.post("/v1/chat/completions", json={"messages": "nope"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert set(error) == {"type", "code", "message", "request_id"}
    assert (
        error["code"] == "invalid_request" and error["request_id"] == resp.headers["x-request-id"]
    )


async def test_upstream_http_error_is_sanitised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Traceback: secret at http://10.0.0.7/internal")

    async with gateway_client(httpx.MockTransport(handler)) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
    assert "10.0.0.7" not in resp.text and "Traceback" not in resp.text


async def test_upstream_connection_failure_is_sanitised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused by db.internal:5432")

    async with gateway_client(httpx.MockTransport(handler)) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    assert resp.status_code == 502 and "db.internal" not in resp.text


async def test_mid_stream_upstream_failure_emits_error_event_and_closes() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield sse_chunk("reach me at ann@exam")
        raise httpx.ReadError("connection reset by 10.1.2.3")

    stream = LazyStream(producer())
    async with gateway_client(lazy_transport(stream)) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    events = parse_sse(resp.text)
    assert "[DONE]" not in events
    error = events[-1]["error"]
    assert error["code"] == "upstream_stream_failed" and error["request_id"]
    assert "10.1.2.3" not in resp.text and "ann@exam" not in resp.text
    assert stream.closed


async def test_malformed_upstream_chunk_is_not_passed_through() -> None:
    async def producer() -> AsyncIterator[bytes]:
        yield sse_chunk("hello ")
        yield b"data: {not json 123-45-6789\n\n"

    async with gateway_client(lazy_transport(LazyStream(producer()))) as client:
        resp = await client.post("/v1/chat/completions", json=BODY)
    assert "123-45-6789" not in resp.text
    assert parse_sse(resp.text)[-1]["error"]["code"] == "upstream_stream_failed"


async def test_unhandled_exception_returns_standard_error(caplog: pytest.LogCaptureFixture) -> None:
    from task3_llm_guardrail import app as app_module

    def boom(chunk: object) -> object:
        raise RuntimeError("secret internal detail")

    transport = httpx.ASGITransport(app=create_mock_llm())
    async with running_app(transport) as app:
        original = app_module.ChatRequest.model_validate_json
        app_module.ChatRequest.model_validate_json = boom  # type: ignore[assignment,method-assign]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://gateway",
            ) as client:
                with caplog.at_level(logging.ERROR):
                    resp = await client.post("/v1/chat/completions", json=BODY)
        finally:
            app_module.ChatRequest.model_validate_json = original  # type: ignore[method-assign]
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in resp.text
    assert "secret internal detail" in caplog.text


async def test_client_disconnect_cancels_upstream() -> None:
    total = 1000

    async def producer() -> AsyncIterator[bytes]:
        for i in range(total):
            await asyncio.sleep(0.005)
            yield sse_chunk(f"word{i} ")
        yield b"data: [DONE]\n\n"

    stream = LazyStream(producer())
    async with running_app(lazy_transport(stream)) as app:
        received = await asyncio.wait_for(call_asgi(app, BODY, disconnect_after=3), timeout=10)
    assert received >= 3
    assert stream.closed, "upstream response was not closed after client disconnect"
    assert stream.produced < total / 2, "upstream kept producing after the client went away"


async def test_large_stream_does_not_accumulate_in_gateway_memory() -> None:
    chunk_text = ("lorem ipsum dolor sit amet, mail jane@example.org now " * 20)[:1000]
    chunks = 5 * 1024  # ~5 MB of content

    async def producer() -> AsyncIterator[bytes]:
        for _ in range(chunks):
            yield sse_chunk(chunk_text)
        yield b"data: [DONE]\n\n"

    seen = {"bytes": 0, "leaks": 0}

    def on_body(body: bytes) -> None:
        seen["bytes"] += len(body)
        seen["leaks"] += body.count(b"jane@example.org")

    async with running_app(lazy_transport(LazyStream(producer()))) as app:
        tracemalloc.start()
        try:
            await call_asgi(app, BODY, on_body=on_body)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert seen["bytes"] > 5 * 1024 * 1024
    assert seen["leaks"] == 0
    assert peak < 2 * 1024 * 1024, f"peak traced memory {peak} bytes"


def test_sse_event_framing_is_exact() -> None:
    from task3_llm_guardrail.sse import format_event, parse_data_line

    assert format_event({"a": 1}) == 'data: {"a":1}\n\n'
    assert format_event("[DONE]") == "data: [DONE]\n\n"
    assert parse_data_line("data: hello") == "hello"
    assert parse_data_line("data:hello") == "hello"
    assert parse_data_line(": keep-alive") is None and parse_data_line("") is None
    assert json.loads(parse_data_line('data: {"x": 1}') or "") == {"x": 1}
