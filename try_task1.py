"""Manual check for Task 1: starts the MCP server and calls its tools with good and bad input.

Run:  python try_task1.py
Edit the CASES list to try your own inputs.
"""

import asyncio
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

GOOD_REASON = "Item arrived damaged"
CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("valid customer lookup", "get_customer_record", {"customer_id": "CUST-10001"}),
    ("customer not found", "get_customer_record", {"customer_id": "CUST-99999"}),
    ("bad id format (lowercase)", "get_customer_record", {"customer_id": "cust-12345"}),
    ("bad id format (4 digits)", "get_customer_record", {"customer_id": "CUST-1234"}),
    ("extra field", "get_customer_record", {"customer_id": "CUST-10001", "admin": True}),
    ("valid refund", "trigger_refund", {"customer_id": "CUST-10001", "amount": 10.5, "reason": GOOD_REASON}),
    ("refund more than balance", "trigger_refund", {"customer_id": "CUST-10004", "amount": 5, "reason": GOOD_REASON}),
    ("amount = 0", "trigger_refund", {"customer_id": "CUST-10001", "amount": 0, "reason": GOOD_REASON}),
    ("amount is a string", "trigger_refund", {"customer_id": "CUST-10001", "amount": "10", "reason": GOOD_REASON}),
    ("amount is true", "trigger_refund", {"customer_id": "CUST-10001", "amount": True, "reason": GOOD_REASON}),
    ("reason too short", "trigger_refund", {"customer_id": "CUST-10001", "amount": 5, "reason": "short"}),
    ("unknown tool", "delete_everything", {}),
]


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "task1_mcp_server.server"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        print("tools:", [t.name for t in tools], "\n")
        for label, name, args in CASES:
            try:
                result = await session.call_tool(name, args)
                kind = "TOOL ERROR (isError)" if result.is_error else "OK"
                print(f"{label:28} -> {kind}: {result.content[0].text}")  # type: ignore[union-attr]
            except MCPError as exc:
                print(f"{label:28} -> JSON-RPC ERROR {exc.code}: {exc.message}")


if __name__ == "__main__":
    asyncio.run(main())
