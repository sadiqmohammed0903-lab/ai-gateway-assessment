import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp_types as types
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError


@asynccontextmanager
async def open_session() -> AsyncIterator[ClientSession]:
    """Spawn the real server as a subprocess and speak MCP over its stdio."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "task1_mcp_server.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


def text_of(result: Any) -> str:
    return str(result.content[0].text)


async def test_tools_list_publishes_validation_rules() -> None:
    async with open_session() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert set(tools) == {"get_customer_record", "trigger_refund"}
    refund = tools["trigger_refund"].input_schema["properties"]
    assert refund["customer_id"]["pattern"] == r"^CUST-[0-9]{5}$"
    assert refund["amount"]["exclusiveMinimum"] == 0
    assert refund["reason"]["minLength"] == 10


async def test_get_customer_record_ok_and_not_found() -> None:
    async with open_session() as session:
        ok = await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        missing = await session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert not ok.is_error and "Ada Lovelace" in text_of(ok)
    assert missing.is_error and "customer_not_found" in text_of(missing)


@pytest.mark.parametrize(
    "arguments",
    [
        {"customer_id": "cust-12345"},
        {"customer_id": "CUST-1234"},
        {"customer_id": 12345},
        {},
        {"customer_id": "CUST-12345", "extra": 1},
    ],
)
async def test_invalid_get_customer_is_jsonrpc_error(arguments: dict[str, Any]) -> None:
    async with open_session() as session:
        with pytest.raises(MCPError) as exc:
            await session.call_tool("get_customer_record", arguments)
    assert exc.value.code == types.INVALID_PARAMS == -32602
    assert "get_customer_record" in exc.value.message


async def test_refund_flow() -> None:
    reason = "Duplicate charge on invoice"
    async with open_session() as session:
        ok = await session.call_tool(
            "trigger_refund", {"customer_id": "CUST-10002", "amount": 100.25, "reason": reason}
        )
        too_much = await session.call_tool(
            "trigger_refund", {"customer_id": "CUST-10004", "amount": 5, "reason": reason}
        )
        unknown = await session.call_tool(
            "trigger_refund", {"customer_id": "CUST-77777", "amount": 5, "reason": reason}
        )
    assert not ok.is_error and "refund_id" in text_of(ok) and "1100.25" in text_of(ok)
    assert too_much.is_error and "refund_exceeds_balance" in text_of(too_much)
    assert unknown.is_error and "customer_not_found" in text_of(unknown)


GOOD = {"customer_id": "CUST-10001", "amount": 5, "reason": "long enough reason"}


@pytest.mark.parametrize(
    ("arguments", "bad_fields"),
    [
        (GOOD | {"amount": 0}, ["amount"]),
        (GOOD | {"amount": -5}, ["amount"]),
        (GOOD | {"amount": "5"}, ["amount"]),
        (GOOD | {"amount": True}, ["amount"]),
        (GOOD | {"reason": "too short"}, ["reason"]),
        ({"customer_id": "bad", "amount": -1, "reason": "x"}, ["amount", "customer_id", "reason"]),
    ],
)
async def test_invalid_refund_is_jsonrpc_error(
    arguments: dict[str, Any], bad_fields: list[str]
) -> None:
    async with open_session() as session:
        with pytest.raises(MCPError) as exc:
            await session.call_tool("trigger_refund", arguments)
    assert exc.value.code == -32602
    assert sorted(e["field"] for e in exc.value.data["errors"]) == bad_fields
    for field in bad_fields:
        assert field in exc.value.message


async def test_unknown_tool_is_jsonrpc_error_not_tool_result() -> None:
    async with open_session() as session:
        with pytest.raises(MCPError) as exc:
            await session.call_tool("delete_everything", {})
    assert exc.value.code == -32602
    assert "Unknown tool" in exc.value.message
