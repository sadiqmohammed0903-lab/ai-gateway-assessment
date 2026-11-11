"""Minimal JSON-RPC MCP server used as the gateway's downstream. Records every call."""

import json
from typing import Any

from fastapi import FastAPI, Request, Response

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Fetch a customer",
        "inputSchema": {"type": "object"},
    },
    {"name": "trigger_refund", "description": "Issue a refund", "inputSchema": {"type": "object"}},
    {
        "name": "admin_reset_key",
        "description": "Rotate an API key (admin)",
        "inputSchema": {"type": "object"},
    },
]


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def create_mock_downstream() -> FastAPI:
    app = FastAPI(title="mock-mcp-downstream")
    app.state.calls = []

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        raw = await request.body()
        message = json.loads(raw)
        app.state.calls.append(
            {
                "message": message,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        method, id_ = message.get("method"), message.get("id")
        if "id" not in message:
            return Response(status_code=202)
        headers: dict[str, str] = {}
        if method == "initialize":
            headers["Mcp-Session-Id"] = "mock-session-1"
            body = _result(
                id_,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-downstream", "version": "0"},
                },
            )
        elif method == "tools/list":
            body = _result(id_, {"tools": TOOLS})
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name")
            if name not in {t["name"] for t in TOOLS}:
                body = _error(id_, -32602, f"Unknown tool: {name}")
            else:
                text = f"{name} executed"
                body = _result(id_, {"content": [{"type": "text", "text": text}], "isError": False})
        else:
            body = _error(id_, -32601, "Method not found")
        return Response(json.dumps(body), media_type="application/json", headers=headers)

    return app


app = create_mock_downstream()
