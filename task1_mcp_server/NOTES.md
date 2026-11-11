# Task 1 – MCP server with strict validation (stdio)

## 1. What was built
- `models.py`: strict Pydantic v2 models (`strict=True`, `extra="forbid"`) for `get_customer_record` and `trigger_refund`. Their JSON schemas are what `tools/list` publishes (`pattern`, `exclusiveMinimum`, `maximum`, `minLength`, `additionalProperties: false`).
- `data.py`: in-memory store with 6 customers (Decimal balances); a refund decrements the balance and returns a `refund_id`.
- `server.py`: low-level `mcp.server.lowlevel.Server` over `stdio_server()`, run with `python -m task1_mcp_server.server`.
- Tests: model unit tests, subprocess integration tests through the SDK stdio client, and raw-pipe stdout purity tests (including a deliberately noisy server).

## 2. Key design decisions and trade-offs
- **Why the low-level `Server`, not `MCPServer` (SDK 2.3.0).** I read the installed source. `MCPServer._handle_call_tool` re-raises only `MCPError`; every other exception, including argument-validation failures and unknown tools, becomes `CallToolResult(is_error=True)`. So the high-level server can never produce a JSON-RPC `-32602` for bad input. A custom `tools/call` handler that raises `MCPError(INVALID_PARAMS, ...)` does. `test_invalid_*_is_jsonrpc_error` and `test_unknown_tool_is_jsonrpc_error_not_tool_result` prove this: the client sees `MCPError` raised, not a tool result.
- **Protocol vs business errors.** Malformed arguments and unknown tools → JSON-RPC error `-32602` with a message naming the failing fields and `data.errors`. Customer not found / refund exceeds balance → normal result with `isError=true`, because the request was valid and the model can react to the failure. Unexpected exceptions → `-32603 Internal error`, details only on stderr.
- **Customer id pattern** is a single constant: `^CUST-[0-9]{5}$`. I used `[0-9]` rather than `\d` because `\d` also matches Unicode digits (a test covers fullwidth digits). No whitespace stripping on ids: `" CUST-12345"` is rejected.
- **Amount**: finite float, `0 < amount <= 10000`, at most 2 decimals (checked via `Decimal(str(v))`). Strict mode rejects booleans and numeric strings; integers such as `25` are accepted as valid numbers. Money is converted to `Decimal` before touching balances.
- **Reason**: stripped, then `min_length=10` (max 500). Published `minLength` is 10; the strip is described in the field description.
- **Error messages never echo rejected values** (could contain PII); only field names and Pydantic rule messages.
- **STDIO isolation**: all logging goes to stderr via `common`. `stdio_server()` in this SDK diverts fd 1 to stderr while serving, so stray `print()` and raw fd writes miss the wire. The test found a real gap: buffered stray output was flushed onto the restored stdout at interpreter exit. `serve()` now flushes `sys.stdout` in a `finally` while the diversion is still active. Ruff `T201` forbids `print` in source.

## 3. Known limitations
- Stdio only; no streamable-HTTP transport.
- Store is in-memory and not persisted; a refund is not idempotent (no idempotency key).
- No auth or per-caller authorization; for stdio the process boundary is the trust boundary.
- The strict float accepts ints; `1.005`-style values are rejected by design rather than rounded.
- If a client closes stdin right after sending, in-flight requests are dropped on shutdown (normal SDK behaviour); the raw test keeps stdin open until replies arrive.
- I could not run MCP Inspector here: Node/npx is not installed on this machine.

## 4. Likely follow-up questions
1. **Why not just use `MCPServer` and `@tool`?** In SDK 2.3.0 it turns validation failures into `isError` results, so a client never sees a protocol-level `-32602`. I needed the low-level handler to raise `MCPError`.
2. **When is it a JSON-RPC error vs `isError`?** JSON-RPC error when the request itself is malformed (bad params, unknown tool). `isError` when the call was valid but the operation failed (not found, insufficient balance) so the LLM can read and recover.
3. **How do you guarantee stdout stays clean?** Logs go to stderr, `T201` bans `print`, the SDK diverts fd 1 while serving, and I flush buffered stdout before the wire is restored. A test runs a server that prints and raw-writes to fd 1 and asserts every stdout line is JSON-RPC.
4. **Why Decimal for money and why `[0-9]` instead of `\d`?** Floats drift; Decimal avoids rounding bugs on balances. `\d` matches Unicode digits, which would let `CUST-１２３４５` through.
5. **What would you add for production?** Idempotency keys on refunds, an audit log, per-caller authorization, persistent storage with transactions, and a streamable-HTTP transport behind the Task 2 gateway.
