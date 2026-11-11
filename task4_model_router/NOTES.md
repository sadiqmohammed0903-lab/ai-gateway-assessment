# Task 4 – Rate-limiting and model fallback router

## 1. What was built
- `app.py`: `POST /v1/completions`. Flow: `X-API-Key` → tenant (SHA-256 of the key looked up in SQLite) → validate body → count tokens → **reserve** in the limiter → route → **settle** or **release**. Global handlers turn every error into the standard schema.
- `db.py`: on-disk SQLite via `aiosqlite`, WAL, `busy_timeout=5000`, a pool of 4 autocommit connections; tables `tenants`, `api_keys`, `token_events(id, tenant_id, ts, tokens, request_id, status)` with index `(tenant_id, ts)`.
- `rate_limiter.py`: token-aware sliding window (50,000 tokens / 60 s per tenant, configurable), injectable clock.
- `router.py` + `providers.py`: primary with a hard 3000 ms `asyncio.timeout`, failover to the secondary, small circuit breaker (off by default, `BREAKER_THRESHOLD`).
- `tokens.py`: tiktoken (`cl100k_base`) with an over-counting fallback if the vocabulary cannot be loaded offline.
- `mock_providers.py`: two FastAPI apps with modes `normal | full_usage | slow | rate_limited | error | garbage | bad_request`, switchable per test or at runtime via `POST /_mode`.

## 2. Key design decisions and trade-offs
- **Sliding window, exact.** Usage = `SUM(tokens)` over rows with `ts > now - 60`. Each admitted request is one row, so the window is truly rolling (not fixed buckets). `Retry-After` is computed by walking the oldest rows until enough tokens would have expired, so it is the real time to capacity, not a constant.
- **Reserve then reconcile.** Reserved = prompt tokens (tiktoken) + `max_tokens`, so a burst of large requests cannot all slip in before any of them finishes. After the response the row is rewritten to the provider-reported usage (the unused part is released); on total failure the row goes to 0 (`released`) so a failing upstream does not burn the tenant's quota. The row keeps its original timestamp.
- **Atomic check-and-reserve.** A per-tenant `asyncio.Lock` serialises coroutines in this process (cheap, avoids busy-waiting), and `BEGIN IMMEDIATE` takes SQLite's write lock so concurrent connections or even other worker processes cannot interleave between the `SUM` and the `INSERT`. One shared connection would not work with explicit transactions (two tasks would share one transaction), hence the connection pool. Proof: 50 concurrent reservations admit exactly 25 × 2,000 = 50,000 (limiter level and HTTP level). A mutation run without lock and transaction makes all three concurrency tests fail.
- **Eviction.** Expired rows never count toward the sum, so correctness does not depend on deleting them. They are deleted for the tenant on every reservation, and a background task (`cleanup_loop`, every 30 s) deletes them for all tenants so idle tenants do not grow the table.
- **Request larger than the whole budget** returns 400 `request_too_large` instead of 429, because waiting would never help.
- **Failover policy.** Fail over on: 429, 408, any 5xx, timeout, connection error and an unparseable 200 body (all signals that the provider is unhealthy or throttling *us*). Do **not** fail over on other 4xx: a 400/401/422 is deterministic, the backup would reject the same request (or mask our own bad credentials), and retrying only doubles cost. Those return 400 `provider_rejected_request` with the reservation released.
- **Timeout race.** `asyncio.timeout(3.0)` wraps the provider call and cancels it when it fires (closing the connection); the httpx timeout is set above it so the asyncio timeout always wins. Tests assert the mock saw the cancellation, no tasks leak, and the failover completes in 3.0–4.5 s. The secondary has its own (longer, configurable) timeout.
- **Circuit breaker (kept small).** After N consecutive primary failures the primary is skipped for a cooldown, then one call probes it. 4xx client errors do not count as failures. Uses the injected clock, so it is tested without sleeping.
- **Error sanitisation.** All errors use `{"error": {"type","code","message","request_id"}}`: validation (400, no FastAPI 422 body), 401, 404/405, 429, 502-class upstream failures (as 503), and a catch-all 500 handler. Provider bodies, stack traces, hostnames, SQL errors and keys are logged to stderr with the request id and never returned. `X-Gateway-Provider` only exposes `primary` or `secondary`, never a URL.
- **Auth.** API keys are stored hashed (SHA-256), never in plaintext; seeded idempotently at startup.

## 3. Known limitations
- Rate limiting is per process-set sharing one SQLite file; it is not suitable for many hosts (use Redis for that). SQLite serialises writers, so write throughput is bounded (fine for thousands of req/s, not tens of thousands).
- The prompt is counted with `cl100k_base`; other model families tokenise differently, and the provider-reported usage is trusted at settlement.
- Settlement keeps the reservation timestamp, so a long request's tokens expire from the window by start time rather than completion time.
- The circuit breaker is per process and in memory; half-open allows several concurrent probes.
- No streaming responses, retries with backoff on the same provider, or hedged requests.
- tiktoken downloads its vocabulary on first use; the offline fallback over-counts (len/3) on purpose.

## 4. Likely follow-up questions
1. **Why reserve before the call and reconcile after?** The real token count is only known afterwards; without a reservation, many concurrent requests would all pass the check and overshoot the limit. Reserving `prompt + max_tokens` is the worst case, and settling returns the unused part.
2. **How is the check-and-reserve atomic?** A per-tenant `asyncio.Lock` for in-process ordering plus `BEGIN IMMEDIATE` for the database-level write lock; the sum and the insert happen in one transaction. Test: 50 parallel requests admit exactly the budget, and removing the lock/transaction makes the test fail.
3. **When do you fail over, and when not?** Fail over on 429/408/5xx/timeouts/connection errors/garbage bodies; not on other 4xx because they are request or credential errors that would fail on the backup too and would waste money.
4. **How do you avoid leaked tasks or connections on a timeout?** `asyncio.timeout` cancels the awaiting call, which closes the in-flight HTTP request; the test checks the provider saw the cancellation and `asyncio.all_tasks()` is unchanged.
5. **How would you scale this to many gateway instances?** Move the window to Redis (sorted sets with a Lua script for atomic check-and-reserve), keep the circuit breaker state per instance or share it, add Prometheus metrics (429 rate, failover rate, timeouts per provider) and tracing, and load keys from a secrets manager.
