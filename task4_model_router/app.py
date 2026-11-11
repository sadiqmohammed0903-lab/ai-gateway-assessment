"""POST /v1/completions: per-tenant auth, token rate limit, primary/secondary routing."""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from common import configure_logging, make_error, set_request_id
from task4_model_router.db import Database
from task4_model_router.providers import CompletionRequest, HttpProvider, ProviderRejected
from task4_model_router.rate_limiter import (
    Clock,
    RateLimitExceeded,
    RequestTooLarge,
    SlidingWindowLimiter,
)
from task4_model_router.router import AllProvidersFailed, CircuitBreaker, ModelRouter
from task4_model_router.tokens import TokenCounter

logger = logging.getLogger("task4.gateway")

DEFAULT_SEED = (("tenant-a", "demo-key-tenant-a"), ("tenant-b", "demo-key-tenant-b"))


@dataclass(frozen=True)
class Settings:
    database_path: str = "gateway.db"
    tokens_per_minute: int = 50_000
    window_seconds: float = 60.0
    primary_url: str = "http://127.0.0.1:9301/v1/completions"
    secondary_url: str = "http://127.0.0.1:9302/v1/completions"
    primary_timeout: float = 3.0
    secondary_timeout: float = 10.0
    primary_api_key: str = ""
    secondary_api_key: str = ""
    cleanup_interval: float = 30.0
    breaker_threshold: int = 0  # 0 disables the circuit breaker
    breaker_cooldown: float = 30.0
    seed_tenants: tuple[tuple[str, str], ...] = field(default=DEFAULT_SEED)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_path=env.get("DATABASE_PATH", cls.database_path),
            tokens_per_minute=int(env.get("TOKENS_PER_MINUTE", cls.tokens_per_minute)),
            primary_url=env.get("PRIMARY_URL", cls.primary_url),
            secondary_url=env.get("SECONDARY_URL", cls.secondary_url),
            primary_timeout=float(env.get("PRIMARY_TIMEOUT_MS", "3000")) / 1000,
            primary_api_key=env.get("PRIMARY_API_KEY", ""),
            secondary_api_key=env.get("SECONDARY_API_KEY", ""),
            breaker_threshold=int(env.get("BREAKER_THRESHOLD", "0")),
        )


class CompletionBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    max_tokens: int = Field(default=256, ge=1, le=32_768)
    model: str | None = Field(default=None, max_length=100)


def _error(
    status: int,
    type_: str,
    code: str,
    message: str,
    rid: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = make_error(type_, code, message, rid).model_dump()
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": rid, **(headers or {})})


def create_app(
    settings: Settings | None = None,
    primary_transport: httpx.AsyncBaseTransport | None = None,
    secondary_transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.time,
    token_counter: TokenCounter | None = None,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    counter = token_counter or TokenCounter()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(cfg.database_path)
        await db.start()
        for tenant_id, api_key in cfg.seed_tenants:
            await db.seed_tenant(tenant_id, tenant_id, api_key)
        limiter = SlidingWindowLimiter(db, cfg.tokens_per_minute, cfg.window_seconds, clock)
        timeout = httpx.Timeout(cfg.primary_timeout + 2, connect=2.0)
        async with (
            httpx.AsyncClient(timeout=timeout, transport=primary_transport) as primary_client,
            httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.secondary_timeout + 2, connect=2.0),
                transport=secondary_transport,
            ) as secondary_client,
        ):
            breaker = (
                CircuitBreaker(cfg.breaker_threshold, cfg.breaker_cooldown, clock)
                if cfg.breaker_threshold
                else None
            )
            app.state.db = db
            app.state.limiter = limiter
            app.state.router = ModelRouter(
                HttpProvider("primary", cfg.primary_url, primary_client, cfg.primary_api_key),
                HttpProvider(
                    "secondary", cfg.secondary_url, secondary_client, cfg.secondary_api_key
                ),
                cfg.primary_timeout,
                cfg.secondary_timeout,
                breaker,
            )
            cleanup = asyncio.create_task(limiter.cleanup_loop(cfg.cleanup_interval))
            try:
                yield
            finally:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
                await db.close()

    app = FastAPI(title="llm-router-gateway", lifespan=lifespan)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = set_request_id(request.headers.get("x-request-id"))
        logger.error("unhandled %s: %s", type(exc).__name__, exc, exc_info=exc)
        return _error(500, "gateway_error", "internal_error", "Internal gateway error", rid)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = set_request_id(request.headers.get("x-request-id"))
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, "gateway_error", code, "Request could not be routed", rid)

    @app.post("/v1/completions", response_model=None)
    async def completions(request: Request) -> JSONResponse:
        rid = set_request_id(request.headers.get("x-request-id"))

        api_key = request.headers.get("x-api-key", "")
        tenant_id = await request.app.state.db.tenant_for_key(api_key) if api_key else None
        if tenant_id is None:
            return _error(
                401, "authentication_error", "invalid_api_key", "Invalid or missing API key", rid
            )

        try:
            body = CompletionBody.model_validate_json(await request.body())
        except ValidationError as exc:
            fields = ", ".join(
                sorted({".".join(map(str, e["loc"])) or "body" for e in exc.errors()})
            )
            return _error(
                400, "invalid_request_error", "invalid_request", f"Invalid request: {fields}", rid
            )

        reserve_tokens = counter.count(body.prompt) + body.max_tokens
        limiter: SlidingWindowLimiter = request.app.state.limiter
        try:
            reservation = await limiter.reserve(tenant_id, reserve_tokens, rid)
        except RateLimitExceeded as exc:
            return _error(
                429,
                "rate_limit_error",
                "rate_limit_exceeded",
                f"Token rate limit exceeded. Retry after {exc.retry_after} seconds.",
                rid,
                {"Retry-After": str(exc.retry_after)},
            )
        except RequestTooLarge:
            return _error(
                400,
                "invalid_request_error",
                "request_too_large",
                "Request exceeds the per-minute token budget",
                rid,
            )

        router: ModelRouter = request.app.state.router
        try:
            result = await router.complete(
                CompletionRequest(body.prompt, body.max_tokens, body.model)
            )
        except ProviderRejected:
            await limiter.release(reservation)
            return _error(
                400,
                "invalid_request_error",
                "provider_rejected_request",
                "The model provider rejected the request",
                rid,
            )
        except AllProvidersFailed:
            await limiter.release(reservation)
            return _error(
                503,
                "service_unavailable",
                "all_providers_unavailable",
                "No model provider is currently available",
                rid,
                {"Retry-After": "5"},
            )
        except BaseException:
            await asyncio.shield(limiter.release(reservation))
            raise

        prompt_tokens = (
            result.prompt_tokens
            if result.prompt_tokens is not None
            else reserve_tokens - body.max_tokens
        )
        completion_tokens = result.completion_tokens
        if completion_tokens is None:
            completion_tokens = counter.count(result.text)
        try:
            await limiter.settle(reservation, prompt_tokens + completion_tokens)
        except Exception:
            logger.exception("failed to settle token reservation")

        payload: dict[str, Any] = {
            "id": rid,
            "object": "text_completion",
            "choices": [{"index": 0, "text": result.text, "finish_reason": result.finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        return JSONResponse(
            payload, headers={"X-Gateway-Provider": result.provider, "X-Request-ID": rid}
        )

    return app


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "9300")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
