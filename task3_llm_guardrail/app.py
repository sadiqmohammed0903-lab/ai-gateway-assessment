"""LLM gateway endpoint that streams an upstream completion back with PII redacted in flight."""

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from common import configure_logging, make_error, set_request_id
from task3_llm_guardrail.redactor import redact_text
from task3_llm_guardrail.sse import DONE, ChunkRedactor, format_event, parse_data_line

logger = logging.getLogger("task3.gateway")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@dataclass(frozen=True)
class Settings:
    upstream_url: str = "http://127.0.0.1:9101/v1/chat/completions"
    upstream_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_url=os.environ.get("UPSTREAM_URL") or cls.upstream_url,
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY", ""),
        )


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


class UpstreamStreamError(Exception):
    pass


def _error(status: int, code: str, message: str, rid: str) -> JSONResponse:
    body = make_error("gateway_error", code, message, rid).model_dump()
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": rid})


def _redact_completion(payload: dict[str, Any]) -> dict[str, Any]:
    for choice in payload.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = redact_text(message["content"])
    return payload


async def stream_redacted(resp: httpx.Response, rid: str) -> AsyncIterator[str]:
    """Re-emit upstream SSE with delta.content redacted; closes the upstream on any exit."""
    redactor = ChunkRedactor()
    try:
        async for line in resp.aiter_lines():
            data = parse_data_line(line)
            if data is None:
                continue
            data = data.strip()
            if data == DONE:
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                raise UpstreamStreamError("malformed chunk") from None
            if not isinstance(chunk, dict):
                raise UpstreamStreamError("unexpected chunk type")
            out = redactor.transform(chunk)
            if out is not None:
                yield format_event(out)
        for held in redactor.flush_chunks():
            yield format_event(held)
        yield format_event(DONE)
    except (httpx.HTTPError, UpstreamStreamError) as exc:
        # Fail closed: the held-back tail of a truncated stream may be a prefix of PII
        # (half an email, part of a card number), so it is dropped rather than flushed.
        logger.warning("upstream stream failed: %s", type(exc).__name__)
        error = make_error("gateway_error", "upstream_stream_failed", "Upstream stream failed", rid)
        yield format_event(error.model_dump())
    finally:
        await resp.aclose()


def create_app(
    settings: Settings | None = None, upstream_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timeout = httpx.Timeout(60.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=upstream_transport) as client:
            app.state.client = client
            yield

    app = FastAPI(title="llm-guardrail-gateway", lifespan=lifespan)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = set_request_id(request.headers.get("x-request-id"))
        logger.exception("unhandled error")
        return _error(500, "internal_error", "Internal gateway error", rid)

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> Response:
        rid = set_request_id(request.headers.get("x-request-id"))
        raw = await request.body()
        try:
            chat_request = ChatRequest.model_validate_json(raw)
        except ValidationError:
            return _error(400, "invalid_request", "Request body is not a valid chat request", rid)

        headers = {"content-type": "application/json"}
        if cfg.upstream_api_key:
            headers["authorization"] = f"Bearer {cfg.upstream_api_key}"
        client: httpx.AsyncClient = request.app.state.client
        upstream_request = client.build_request(
            "POST", cfg.upstream_url, content=raw, headers=headers
        )
        try:
            resp = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream connection failed: %s", type(exc).__name__)
            return _error(502, "upstream_unavailable", "Upstream model is unavailable", rid)
        if resp.status_code >= 400:
            await resp.aclose()
            logger.warning("upstream returned status %s", resp.status_code)
            return _error(502, "upstream_error", "Upstream model returned an error", rid)

        if not chat_request.stream:
            try:
                payload = json.loads(await resp.aread())
            except (ValueError, httpx.HTTPError):
                return _error(502, "upstream_error", "Upstream model returned an error", rid)
            finally:
                await resp.aclose()
            return JSONResponse(_redact_completion(payload), headers={"X-Request-ID": rid})

        return StreamingResponse(
            stream_redacted(resp, rid),
            media_type="text/event-stream",
            headers={**SSE_HEADERS, "X-Request-ID": rid},
        )

    return app


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "9100")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
