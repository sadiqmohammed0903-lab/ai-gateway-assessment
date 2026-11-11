"""MCP server (stdio) exposing get_customer_record and trigger_refund.

Built on the low-level `Server` rather than `MCPServer`: the high-level server converts every
non-MCPError exception (including argument validation and unknown tools) into an isError tool
result, so it can never emit a JSON-RPC -32602. Here protocol problems raise `MCPError`, which
the SDK serialises as a real JSON-RPC error; business failures return isError results.
"""

import json
import logging
import sys
from decimal import Decimal
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from common import configure_logging
from task1_mcp_server.data import (
    CustomerNotFoundError,
    CustomerStore,
    RefundExceedsBalanceError,
)
from task1_mcp_server.models import (
    GetCustomerRecordInput,
    TriggerRefundInput,
    format_errors,
)

logger = logging.getLogger("task1.server")

SERVER_NAME = "customer-support-server"
store = CustomerStore()

TOOL_MODELS: dict[str, type[BaseModel]] = {
    "get_customer_record": GetCustomerRecordInput,
    "trigger_refund": TriggerRefundInput,
}
TOOL_DESCRIPTIONS = {
    "get_customer_record": "Fetch a customer record by customer_id (CUST-XXXXX).",
    "trigger_refund": "Issue a refund against a customer's balance.",
}


def _text_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))], is_error=is_error
    )


def _get_customer_record(args: GetCustomerRecordInput) -> types.CallToolResult:
    try:
        customer = store.get(args.customer_id)
    except CustomerNotFoundError:
        return _text_result(
            {"error": "customer_not_found", "customer_id": args.customer_id}, is_error=True
        )
    return _text_result(
        {
            "customer_id": customer.customer_id,
            "name": customer.name,
            "email": customer.email,
            "balance": str(customer.balance),
        }
    )


def _trigger_refund(args: TriggerRefundInput) -> types.CallToolResult:
    amount = Decimal(str(args.amount))
    try:
        refund_id, new_balance = store.refund(args.customer_id, amount)
    except CustomerNotFoundError:
        return _text_result(
            {"error": "customer_not_found", "customer_id": args.customer_id}, is_error=True
        )
    except RefundExceedsBalanceError:
        return _text_result(
            {"error": "refund_exceeds_balance", "customer_id": args.customer_id}, is_error=True
        )
    logger.info("refund issued refund_id=%s customer=%s", refund_id, args.customer_id)
    return _text_result(
        {
            "refund_id": refund_id,
            "customer_id": args.customer_id,
            "amount": str(amount),
            "remaining_balance": str(new_balance),
        }
    )


async def list_tools(
    ctx: Any, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                input_schema=model.model_json_schema(),
            )
            for name, model in TOOL_MODELS.items()
        ]
    )


async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    model = TOOL_MODELS.get(params.name)
    if model is None:
        logger.info("unknown tool requested")
        raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name[:64]}")
    try:
        args = model.model_validate(params.arguments if params.arguments is not None else {})
    except ValidationError as exc:
        errors = format_errors(exc)
        logger.info("invalid arguments for %s: %s", params.name, [e["field"] for e in errors])
        fields = ", ".join(e["field"] for e in errors)
        raise MCPError(
            types.INVALID_PARAMS,
            f"Invalid arguments for {params.name}: {fields}",
            {"errors": errors},
        ) from None
    try:
        if isinstance(args, GetCustomerRecordInput):
            return _get_customer_record(args)
        assert isinstance(args, TriggerRefundInput)
        return _trigger_refund(args)
    except Exception:
        logger.exception("unexpected failure in %s", params.name)
        raise MCPError(types.INTERNAL_ERROR, "Internal error") from None


def build_server() -> Server:
    return Server(SERVER_NAME, version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            # fd 1 is diverted to stderr only while serving. Flush any buffered stray output
            # now, otherwise it would be written to the restored wire at interpreter exit.
            sys.stdout.flush()


def main() -> None:
    configure_logging()
    logger.info("starting %s on stdio", SERVER_NAME)
    anyio.run(serve)


if __name__ == "__main__":
    main()
