# Task 2 – MCP security gateway proxy

## 1. What was built
- `app.py`: FastAPI `POST /mcp`. Flow: authenticate → parse JSON-RPC envelope → authorise by method/tool → forward the original bytes to the downstream with one shared `httpx.AsyncClient` (created in the lifespan).
- `auth.py`: PyJWT, HS256 only, `exp` required, `role` claim must be `admin` or `viewer`. `mint_token.py` is the CLI (`python -m task2_mcp_gateway.mint_token --role admin`).
- `jsonrpc.py`: envelope validation, error builders, tool-name normalisation.
- `mock_downstream.py`: tiny MCP-style JSON-RPC server (`initialize`, `tools/list`, `tools/call`) that records every request, so tests can assert "downstream was never called".
- Tests (53) run fully in-process: the gateway and the downstream are both mounted with `httpx.ASGITransport`.

## 2. Key design decisions and trade-offs
- **Order of checks**: auth first, then body parsing. An unauthenticated caller learns nothing about the JSON-RPC layer.
- **HTTP status mapping**: 401 (+ `WWW-Authenticate: Bearer`) for auth failures with a JSON-RPC error body (code `-32000`, since `-32001` is reserved for the unauthorized-tool case); 400 for `-32700` / `-32600`; **200** for a denied tool call (`-32001`) and for invalid `params.name` (`-32602`), because the transport worked and MCP clients surface the JSON-RPC error properly on a 2xx; 502 / 504 for downstream failures with code `-32603` and the request id preserved.
- **Batches are rejected with `-32600`** for every role. Reason: the MCP spec (2025-06-18) removed JSON-RPC batching, and handling batches means authorising each element and splicing responses, which is extra attack surface for no benefit. The alternative (per-element authorisation) is easy to get wrong: one unchecked element would bypass the policy.
- **Tool-name normalisation before the `admin_` check**: NFKC (folds full-width letters), drop Unicode format characters (zero-width space, soft hyphen), strip whitespace, casefold. The normalised name is used only for the decision; the original bytes are forwarded unchanged. A mutation run (normalisation disabled) makes 8 bypass tests fail, so they do guard the behaviour.
- **Fail closed**: `tools/call` with missing or non-string `params.name` never reaches the downstream (`-32602`). Denied notifications (no `id`) get an empty `202`, since JSON-RPC forbids replying to notifications.
- **Header hygiene**: allow-list forwarding (`Content-Type`, `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version`, plus our `X-Request-ID`). `Authorization` and cookies are never forwarded; hop-by-hop headers are dropped by construction. Response headers are allow-listed too.
- **Audit log** (stderr): `request_id, role, method, tool, outcome`. The token is never logged; method and tool strings are `repr`-quoted and truncated to prevent log injection.
- **Error sanitisation**: downstream connect/timeout errors become a fixed message; exception text and hostnames only appear in logs (as the exception class name).

## 3. Known limitations
- The policy is a deny-prefix rule (`admin_`). The stronger production design is an explicit per-role allow-list of tool names; a downstream that normalises names differently from the gateway is still a risk.
- The downstream response is buffered, not streamed, so SSE (`text/event-stream`) responses from a streamable-HTTP MCP server would be delayed rather than live. Fine for the JSON mode used here.
- Roles come from a symmetric shared secret (HS256); no key rotation, no `kid`, no revocation, and no rate limiting on the gateway.
- `tools/list` is forwarded unfiltered: viewers can still see that admin tools exist. Filtering the list per role would be a natural extension.
- The gateway does not validate tool arguments; that stays the downstream's job.

## 4. Likely follow-up questions
1. **Why 200 for a denied tool call but 401 for a bad token?** A bad token is a transport-level auth failure, so 401 plus `WWW-Authenticate`. A denied tool is a valid, authenticated request that the policy rejects at the JSON-RPC layer, and clients read the error body reliably on a 2xx.
2. **How could a viewer try to bypass the `admin_` check?** Case changes, leading whitespace, full-width Unicode, zero-width characters, a missing or non-string `name`, or hiding the call inside a batch. Each is covered by a test; batches are rejected outright.
3. **Why reject batches instead of supporting them?** The MCP spec dropped them, and per-element authorisation plus response assembly adds risk. If needed I would authorise every element before forwarding any, and reject the whole batch if one is denied.
4. **Why not just forward the token downstream?** It would give the downstream a credential meant for the gateway (token passthrough / confused-deputy risk). The gateway authenticates the user, and the gateway-to-downstream hop should have its own identity (e.g. mTLS or a service token).
5. **How would you harden this for production?** Per-role allow-lists, asymmetric JWTs with key rotation (JWKS), per-tenant rate limits, filtered `tools/list`, streaming pass-through for SSE, and structured audit logs shipped to a SIEM.
