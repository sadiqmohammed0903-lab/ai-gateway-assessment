import json
import logging

import httpx
import pytest

from task2_mcp_gateway.auth import mint_token
from task2_mcp_gateway.tests.conftest import (
    SECRET,
    Env,
    headers_for,
    make_gateway_client,
    rpc,
    tool_call,
)

# ---------- authorised / forwarded flows ----------


async def test_viewer_tools_list_is_forwarded(env: Env) -> None:
    resp = await env.client.post("/mcp", json=rpc("tools/list", 7), headers=headers_for("viewer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 7 and len(body["result"]["tools"]) == 3
    assert len(env.calls) == 1


async def test_viewer_non_admin_tool_is_forwarded(env: Env) -> None:
    resp = await env.client.post(
        "/mcp", json=tool_call("get_customer_record"), headers=headers_for("viewer")
    )
    assert resp.json()["result"]["content"][0]["text"] == "get_customer_record executed"
    assert len(env.calls) == 1


async def test_viewer_admin_tool_denied_and_downstream_never_called(env: Env) -> None:
    resp = await env.client.post(
        "/mcp", json=tool_call("admin_reset_key", id_=42), headers=headers_for("viewer")
    )
    assert resp.json() == {
        "jsonrpc": "2.0",
        "id": 42,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert env.calls == []


async def test_admin_admin_tool_is_forwarded(env: Env) -> None:
    resp = await env.client.post(
        "/mcp", json=tool_call("admin_reset_key"), headers=headers_for("admin")
    )
    assert resp.json()["result"]["content"][0]["text"] == "admin_reset_key executed"
    assert len(env.calls) == 1


# ---------- bypass attempts ----------


@pytest.mark.parametrize(
    "name",
    [
        "ADMIN_reset_key",
        "Admin_Reset_Key",
        " admin_reset_key",
        "\tadmin_reset_key",
        "\nadmin_reset_key",
        "admin_reset_key ",
        "ａｄｍｉｎ_reset_key",  # full-width characters
        "​admin_reset_key",  # zero-width space
        "ad­min_reset_key",  # soft hyphen
    ],
)
async def test_viewer_cannot_bypass_prefix_check(env: Env, name: str) -> None:
    resp = await env.client.post("/mcp", json=tool_call(name), headers=headers_for("viewer"))
    assert resp.json()["error"]["code"] == -32001
    assert env.calls == []


@pytest.mark.parametrize(
    "params",
    [None, {}, {"arguments": {}}, {"name": 123}, {"name": None}, {"name": ["admin_x"]}, [], "x"],
)
async def test_tools_call_with_bad_name_is_invalid_params(env: Env, params: object) -> None:
    message: dict = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
    if params is not None:
        message["params"] = params
    resp = await env.client.post("/mcp", json=message, headers=headers_for("viewer"))
    body = resp.json()
    # params="x" is rejected earlier as an invalid envelope; everything else is -32602
    assert body["error"]["code"] in (-32602, -32600)
    if params != "x":
        assert body["error"]["code"] == -32602 and body["id"] == 3
    assert env.calls == []


# ---------- authentication ----------


async def test_missing_header_is_401(env: Env) -> None:
    resp = await env.client.post("/mcp", json=rpc("tools/list"))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == -32000
    assert env.calls == []


@pytest.mark.parametrize(
    "header",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Bearer not-a-jwt", "token abc"],
)
async def test_malformed_authorization_is_401(env: Env, header: str) -> None:
    resp = await env.client.post("/mcp", json=rpc("tools/list"), headers={"Authorization": header})
    assert resp.status_code == 401 and env.calls == []


async def test_expired_token_is_401(env: Env) -> None:
    resp = await env.client.post(
        "/mcp", json=rpc("tools/list"), headers=headers_for("admin", ttl=-10)
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Token expired"
    assert env.calls == []


async def test_wrong_secret_is_401(env: Env) -> None:
    headers = headers_for("admin", secret="a-completely-different-secret-value-1234")
    resp = await env.client.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert resp.status_code == 401 and env.calls == []


@pytest.mark.parametrize("role", ["superuser", "ADMIN", "", None])
async def test_unknown_role_is_401(env: Env, role: str | None) -> None:
    import jwt

    claims: dict = {"exp": 4_102_444_800}
    if role is not None:
        claims["role"] = role
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    resp = await env.client.post(
        "/mcp", json=rpc("tools/list"), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401 and env.calls == []


async def test_token_without_exp_and_alg_none_are_rejected(env: Env) -> None:
    import jwt

    no_exp = jwt.encode({"role": "admin"}, SECRET, algorithm="HS256")
    unsigned = jwt.encode(
        {"role": "admin", "exp": 4_102_444_800},
        None,  # type: ignore[arg-type]
        algorithm="none",
    )
    for token in (no_exp, unsigned):
        resp = await env.client.post(
            "/mcp", json=rpc("tools/list"), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
    assert env.calls == []


# ---------- JSON-RPC envelope ----------


async def test_malformed_json_is_parse_error(env: Env) -> None:
    resp = await env.client.post(
        "/mcp",
        content=b"{not json",
        headers=headers_for("viewer") | {"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert env.calls == []


@pytest.mark.parametrize(
    ("payload", "expected_id"),
    [
        ({"id": 5, "method": "tools/list"}, 5),
        ({"jsonrpc": "1.0", "id": 6, "method": "tools/list"}, 6),
        ({"jsonrpc": "2.0", "id": 8}, 8),
        ({"jsonrpc": "2.0", "id": 9, "method": 5}, 9),
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "x"}, None),
        ({"jsonrpc": "2.0", "id": True, "method": "x"}, None),
        ({"jsonrpc": "2.0", "id": 1, "method": "x", "params": 3}, 1),
        ("just a string", None),
        (42, None),
    ],
)
async def test_invalid_envelope(env: Env, payload: object, expected_id: object) -> None:
    resp = await env.client.post("/mcp", json=payload, headers=headers_for("viewer"))
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == -32600 and body["id"] == expected_id
    assert env.calls == []


async def test_notification_is_forwarded_without_response_body(env: Env) -> None:
    resp = await env.client.post(
        "/mcp", json=rpc("notifications/initialized", None), headers=headers_for("viewer")
    )
    assert resp.status_code == 202 and resp.content == b""
    assert len(env.calls) == 1 and "id" not in env.calls[0]["message"]


async def test_denied_notification_gets_no_json_rpc_response(env: Env) -> None:
    message = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}
    resp = await env.client.post("/mcp", json=message, headers=headers_for("viewer"))
    assert resp.status_code == 202 and resp.content == b"" and env.calls == []


@pytest.mark.parametrize("role", ["admin", "viewer"])
async def test_batch_is_rejected_for_every_role(env: Env, role: str) -> None:
    batch = [tool_call("admin_reset_key", 1), rpc("tools/list", 2)]
    resp = await env.client.post("/mcp", json=batch, headers=headers_for(role))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600
    assert env.calls == []


# ---------- proxy behaviour ----------


async def test_headers_forwarded_and_token_not_leaked_downstream(env: Env) -> None:
    headers = headers_for("viewer") | {
        "Mcp-Session-Id": "sess-1",
        "MCP-Protocol-Version": "2025-06-18",
    }
    headers["Cookie"] = "secret=1"
    resp = await env.client.post("/mcp", json=rpc("initialize", 1), headers=headers)
    forwarded = env.calls[0]["headers"]
    assert forwarded["mcp-session-id"] == "sess-1"
    assert forwarded["mcp-protocol-version"] == "2025-06-18"
    assert "authorization" not in forwarded and "cookie" not in forwarded
    assert resp.headers["mcp-session-id"] == "mock-session-1"


async def test_downstream_status_and_body_pass_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}}
        )

    client, gateway = await make_gateway_client(httpx.MockTransport(handler))
    async with gateway.router.lifespan_context(gateway), client:
        resp = await client.post("/mcp", json=rpc("weird/method"), headers=headers_for("viewer"))
    assert resp.status_code == 418 and resp.json()["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (httpx.ConnectError("secret-host.internal:5432 refused"), 502),
        (httpx.ReadTimeout("took too long talking to 10.0.0.5"), 504),
    ],
)
async def test_downstream_failure_is_sanitised(exc: Exception, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    client, gateway = await make_gateway_client(httpx.MockTransport(handler))
    async with gateway.router.lifespan_context(gateway), client:
        resp = await client.post("/mcp", json=rpc("tools/list", 11), headers=headers_for("viewer"))
    assert resp.status_code == status
    body = resp.json()
    assert body["id"] == 11 and body["error"]["code"] == -32603
    text = resp.text
    assert "secret-host" not in text and "10.0.0.5" not in text and "Traceback" not in text


# ---------- audit logging ----------


async def test_audit_log_records_decisions_and_never_logs_token(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    token = mint_token("viewer", SECRET)
    headers = {"Authorization": f"Bearer {token}"}
    await env.client.post("/mcp", json=tool_call("admin_reset_key"), headers=headers)
    await env.client.post("/mcp", json=tool_call("get_customer_record"), headers=headers)
    audit = [r.getMessage() for r in caplog.records if r.name == "task2.audit"]
    assert any(
        "role=viewer" in m and "admin_reset_key" in m and "denied:forbidden" in m for m in audit
    )
    assert any("get_customer_record" in m and "outcome=allowed" in m for m in audit)
    assert token not in caplog.text
    assert json.dumps(headers) not in caplog.text
