# Interview prep

## 2-minute walkthrough (say it out loud)

"I built four pieces that sit between AI agents and the systems they talk to, and I mocked every upstream so everything runs offline and is tested.

**First, an MCP server** in Python with two tools, `get_customer_record` and `trigger_refund`. Inputs are validated with strict Pydantic models. The interesting part is error handling: the SDK's high-level server turns every exception into a tool result with `isError`, so a client would never see a real protocol error. I read the SDK source, and used the low-level server so bad arguments and unknown tools come back as JSON-RPC `-32602`, while business failures like 'customer not found' stay normal tool results. stdout is only JSON-RPC; logs go to stderr, and I have a test that runs a server which deliberately prints and checks that nothing reaches stdout. That test actually found a real bug: buffered output was flushed onto the wire at exit.

**Second, an MCP gateway** that checks a JWT, reads the role, and blocks `admin_` tools for viewers with `-32001` *without calling the downstream*. The tests assert the downstream recorded zero calls. I normalise tool names, so `ADMIN_reset_key`, a leading space, full-width characters and zero-width characters can't bypass it. Batches are rejected, the token is never forwarded or logged, and downstream failures return a clean error.

**Third, an LLM gateway that redacts PII while streaming.** PII can be split across chunks, like half an email in one chunk and the rest in the next. I only hold back the tail that could still become a match, usually just the last word, capped at the longest possible match. So memory is bounded and the first token isn't delayed. A property test compares streamed output with redacting the whole text, and a fuzz run of twenty thousand cases found no mismatch. The tests also found two real card-matching bugs, which I fixed.

**Fourth, a router with a token-aware rate limiter** on SQLite. It reserves prompt plus max tokens before the call and reconciles to real usage after. The check-and-reserve is atomic with a per-tenant lock plus `BEGIN IMMEDIATE`; fifty parallel requests admit exactly the budget. If the primary returns 429, errors, or takes more than three seconds, it fails over to the secondary; it does not fail over on a 400 because the backup would reject it too. Every error uses one schema and never leaks upstream details.

If I had more time, I'd move the limiter to Redis, add tracing and metrics, and put mTLS between the gateways and servers."

## Top 10 questions

1. **Why did you use the low-level MCP server instead of the decorator API?**
   The high-level server converts every non-`MCPError` exception, including validation failures and unknown tools, into an `isError` result. I wanted real `-32602` errors for malformed requests, and `isError` only for valid requests whose operation failed, so the model can recover.

2. **How do you keep stdout clean for a stdio MCP server?**
   Logs go to stderr, ruff rule T201 bans `print`, the SDK diverts fd 1 while serving, and I flush buffered stdout before it is restored. A test runs a server that prints and raw-writes to fd 1 and asserts every stdout line is valid JSON-RPC.

3. **How do you stop a viewer reaching an admin tool?**
   Auth first, then the policy on `tools/call`: the name is NFKC-normalised, format characters are dropped, trimmed and casefolded before the `admin_` check. Missing or non-string names are rejected, batches are rejected, and a denied call never reaches the downstream (tested by asserting zero downstream calls). Production would use an explicit per-role allow-list.

4. **Why return HTTP 200 for a denied tool call but 401 for a bad token?**
   A bad token is a transport-level auth failure. A denied tool is an authenticated request refused at the JSON-RPC layer, and MCP clients read the error body reliably on a 2xx.

5. **How do you redact PII split across stream chunks without buffering everything?**
   Each pattern has a "viable prefix" regex; I emit everything before the earliest viable partial match and hold only that tail, capped at the longest possible match. Memory is O(window), and the first token is normally not delayed. A property test proves streamed output equals whole-text redaction.

6. **What are the limits of regex-based redaction, and what would you add?**
   It misses names, addresses and obfuscated forms, and Luhn-valid non-cards are redacted. I'd layer an NER model or a DLP service, redact both prompts and responses, and emit detection metrics without raw values.

7. **How is your rate limiter atomic and accurate?**
   One row per request with a timestamp; usage is the sum over the last 60 seconds. A per-tenant `asyncio.Lock` plus `BEGIN IMMEDIATE` makes the sum and insert one transaction; a pool of connections avoids sharing one transaction between tasks. Fifty concurrent requests admit exactly the budget, and the test fails if I remove the protection.

8. **Why reserve tokens before the call?**
   The real count is only known afterwards; without a reservation concurrent requests all pass the check and overshoot. I reserve prompt plus `max_tokens`, then settle to actual usage, or release everything on failure.

9. **When do you fail over, and when not?**
   On 429, 408, 5xx, timeout, connection error and unparseable responses. Not on other 4xx: they are deterministic request or credential errors, the backup would reject them too, and retrying doubles cost. The timeout uses `asyncio.timeout`, which cancels the in-flight call; a test checks nothing leaks.

10. **How do you make sure errors don't leak internals?**
    One schema with a request id; a global exception handler; provider bodies, stack traces, hosts and SQL errors are logged to stderr only. Tests inject a fake traceback, internal IP and SQL text, then assert they appear in logs and not in any response.

## What I would do next in production

- **Distributed rate limiting:** Redis sorted sets with a Lua script for atomic check-and-reserve across many gateway instances.
- **Observability:** OpenTelemetry traces across client → gateway → provider; Prometheus metrics (429 rate, failover rate, timeouts per provider, redaction counts, TTFT and total latency histograms) with Grafana dashboards and alerts.
- **Secrets management:** provider keys and the JWT signing key from a secrets manager with rotation; asymmetric JWTs (JWKS) instead of a shared secret.
- **mTLS and identity** between gateways, MCP servers and providers, so the gateway authenticates itself rather than passing user tokens through.
- **Policy:** per-role tool allow-lists (and filtered `tools/list`), per-tenant budgets and quotas, idempotency keys on refunds, audit logs shipped to a SIEM.
- **Resilience:** streaming pass-through for SSE in the MCP gateway, backoff and hedged requests, shared circuit-breaker state, load tests for the limiter and guardrail.
- **Delivery:** run the container images in CI, add contract tests against real providers behind env flags, and deploy with health checks and graceful shutdown.
