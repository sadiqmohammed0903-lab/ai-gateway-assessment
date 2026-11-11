"""MCP security gateway: JWT auth and method-level authorisation for a downstream MCP server."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from common import configure_logging, set_request_id
from task2_mcp_gateway import jsonrpc
from task2_mcp_gateway.auth import AuthError, Role, authenticate

logger = logging.getLogger("task2.gateway")
audit = logging.getLogger("task2.audit")

FORWARD_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version")
FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version")


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    downstream_url: str = "http://127.0.0.1:9001/mcp"
    connect_timeout: float = 3.0
    read_timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            jwt_secret=os.environ.get("JWT_SECRET", ""),
            downstream_url=os.environ.get("DOWNSTREAM_URL", cls.downstream_url),
        )


@dataclass
class Decision:
    allowed: bool
    tool: str | None = None
    failure: jsonrpc.JsonRpcFailure | None = None
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def authorise(request: jsonrpc.JsonRpcRequest, role: Role) -> Decision:
    """Method-level policy. Everything except tools/call is forwarded transparently."""
    if request.method != "tools/call":
        return Decision(allowed=True)
    name = jsonrpc.extract_tool_name(request.params)
    if name is None:
        failure = jsonrpc.JsonRpcFailure(
            jsonrpc.INVALID_PARAMS, "Invalid params: params.name must be a string", request.id
        )
        return Decision(allowed=False, failure=failure, reason="invalid_params")
    if jsonrpc.is_admin_tool(name) and role != "admin":
        failure = jsonrpc.JsonRpcFailure(
            jsonrpc.UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call", request.id
        )
        return Decision(allowed=False, tool=name, failure=failure, reason="forbidden")
    return Decision(allowed=True, tool=name)


def _error_response(
    failure: jsonrpc.JsonRpcFailure, status: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = jsonrpc.error_body(failure.id, failure.code, failure.message)
    return JSONResponse(body, status_code=status, headers=headers)


def _audit(rid: str, role: str, method: str, tool: str | None, outcome: str) -> None:
    audit.info(
        "request_id=%s role=%s method=%r tool=%r outcome=%s",
        rid,
        role,
        method[:64],
        (tool or "-")[:64],
        outcome,
    )


def create_app(
    settings: Settings | None = None, downstream_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    cfg = settings or Settings.from_env()
    if not cfg.jwt_secret:
        raise RuntimeError("JWT_SECRET must be set")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        timeout = httpx.Timeout(cfg.read_timeout, connect=cfg.connect_timeout)
        async with httpx.AsyncClient(timeout=timeout, transport=downstream_transport) as client:
            app.state.client = client
            yield

    app = FastAPI(title="mcp-gateway", lifespan=lifespan)

    @app.post("/mcp")
    async def proxy(request: Request) -> Response:
        rid = set_request_id(request.headers.get("x-request-id"))
        rid_header = {"X-Request-ID": rid}

        try:
            role = authenticate(request.headers.get("authorization"), cfg.jwt_secret)
        except AuthError as exc:
            _audit(rid, "-", "-", None, f"denied:auth:{exc}")
            failure = jsonrpc.JsonRpcFailure(jsonrpc.AUTH_REQUIRED, str(exc))
            return _error_response(failure, 401, {**rid_header, "WWW-Authenticate": "Bearer"})

        body = await request.body()
        parsed = jsonrpc.parse_request(body)
        if isinstance(parsed, jsonrpc.JsonRpcFailure):
            _audit(rid, role, "-", None, f"rejected:{parsed.code}")
            return _error_response(parsed, 400, rid_header)

        decision = authorise(parsed, role)
        if not decision.allowed:
            assert decision.failure is not None
            _audit(rid, role, parsed.method, decision.tool, f"denied:{decision.reason}")
            if not parsed.has_id:
                return Response(status_code=202, headers=rid_header)
            return _error_response(decision.failure, 200, rid_header)

        return await _forward(request, body, parsed, role, decision.tool, rid)

    async def _forward(
        request: Request,
        body: bytes,
        parsed: jsonrpc.JsonRpcRequest,
        role: Role,
        tool: str | None,
        rid: str,
    ) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        headers = {h: request.headers[h] for h in FORWARD_REQUEST_HEADERS if h in request.headers}
        headers["x-request-id"] = rid
        try:
            upstream = await client.post(cfg.downstream_url, content=body, headers=headers)
        except httpx.TimeoutException:
            logger.warning("downstream timeout")
            return _downstream_failure(parsed, 504, "Downstream server timed out", role, tool, rid)
        except httpx.HTTPError as exc:
            logger.warning("downstream unavailable: %s", type(exc).__name__)
            return _downstream_failure(
                parsed, 502, "Downstream server unavailable", role, tool, rid
            )

        _audit(rid, role, parsed.method, tool, "allowed")
        out_headers = {
            h: upstream.headers[h] for h in FORWARD_RESPONSE_HEADERS if h in upstream.headers
        }
        out_headers["X-Request-ID"] = rid
        return Response(
            content=upstream.content, status_code=upstream.status_code, headers=out_headers
        )

    def _downstream_failure(
        parsed: jsonrpc.JsonRpcRequest,
        status: int,
        message: str,
        role: Role,
        tool: str | None,
        rid: str,
    ) -> Response:
        _audit(rid, role, parsed.method, tool, f"error:downstream_{status}")
        failure = jsonrpc.JsonRpcFailure(jsonrpc.INTERNAL_ERROR, message, parsed.id)
        return _error_response(failure, status, {"X-Request-ID": rid})

    return app


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "9000")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
