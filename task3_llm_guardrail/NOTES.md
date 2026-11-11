# Task 3 – LLM gateway streaming guardrail (PII redaction)

## 1. What was built
- `app.py`: `POST /v1/chat/completions` (OpenAI request shape). Opens the upstream with `client.send(stream=True)` on one shared `httpx.AsyncClient`, then returns a `StreamingResponse` over an async generator that re-emits SSE with `delta.content` redacted. Non-streaming requests are supported too (the JSON body is redacted).
- `redactor.py`: precompiled patterns (email, US SSN, credit card with Luhn) declared in one `PATTERNS` tuple, plus `StreamRedactor` (incremental) and `redact_text` (whole-string reference).
- `sse.py`: SSE framing and `ChunkRedactor` (one `StreamRedactor` per choice index; preserves `role`, `finish_reason`, ids, usage).
- `mock_llm.py`: OpenAI-style streaming mock that deliberately splits an email, SSN and card number across chunk boundaries. `bench_ttft.py`: p50/p95 TTFT and total latency, direct vs through the gateway.
- Optional real provider: set `UPSTREAM_URL` and `UPSTREAM_API_KEY`; the mock is the default.

## 2. Key design decisions and trade-offs
- **Bounded hold-back, not a fixed delay.** Every pattern declares `max_len` and a `partial` regex ("text at the end of the buffer that could still grow into a match"). On each feed the redactor finds the earliest start of such a tail, emits everything before it (with complete matches replaced), and keeps only the tail. A match starting before that cut either ends before the buffer end (nothing later can change it) or it would itself be a partial tail and is held. The held tail is capped at `WINDOW` = the longest possible match (345 chars, from the email pattern: 64 local + `@` + up to 4 labels + TLD). Memory per stream is O(WINDOW), never O(response).
- **TTFT trade-off.** A naive "always hold W chars" design would delay the first token until ~345 characters have arrived. Here the delay is typically one word or number (`"Hello wor"` emits `"Hello "`), because only text that looks like a possible PII prefix is held. Worst case is a long run that looks like an email/number (≤ WINDOW chars). In the benchmark below the first content chunk is emitted immediately.
- **Equivalence by construction.** `redact_text` (reference) and the streaming path share one validated-match scanner, and a one-character context carry keeps lookbehind assertions (`(?<!\d)`) correct across the cut. A property test checks streamed output == whole-text output for random texts and chunkings; I also ran a one-off 20,000-case fuzz with zero mismatches or leaks.
- **Card matching bugs found by the tests (fixed).** (1) A loose `digits-and-separators` pattern merged a card with neighbouring numbers, failed Luhn as a whole and left the real card visible; it now uses structured shapes (4-4-4-x, 4-6-4/5, plain 13–19) with a consistent separator. (2) When a candidate fails Luhn, scanning retries one character later, so a real card after a stray number is still found.
- **SSN**: `ddd-dd-dddd` or space-separated, excluding invalid areas (000, 666, 9xx), group 00 and serial 0000. Bare 9-digit numbers are not matched (too many false positives).
- **Fail closed on errors.** If the upstream stream breaks mid-way (or sends a malformed chunk) the gateway drops the held tail and emits a standard error event (`upstream_stream_failed`, with `request_id`), then closes. A truncated tail can be a prefix of PII, so it is never flushed. A clean end of stream (with or without `[DONE]`) flushes and redacts the tail first. Held text is also flushed before a `finish_reason` chunk so nothing is lost or reordered.
- **Disconnect handling.** The generator's `finally` closes the upstream response; when Starlette cancels the generator on client disconnect, the upstream request is torn down. Tested with a lazy upstream and a mutation check (disabling the close makes the test fail).
- **Errors** use the common schema; upstream status codes, bodies and hostnames are never exposed (only logged).

## 3. Benchmark (mock chunk delay 20 ms, 8 content chunks, 50 runs, real uvicorn servers, Windows)
| | TTFT p50 / p95 | total p50 / p95 |
|---|---|---|
| direct to mock | 34.8 / 45.2 ms | 260.8 / 274.1 ms |
| through gateway | 45.3 / 66.0 ms | 289.5 / 306.1 ms |

The gateway adds ~10 ms to TTFT and ~29 ms to the whole stream. That is mostly the extra loopback HTTP hop plus both servers sharing one machine; the redactor itself costs ~15 µs per 40-char chunk (~2.7 MB/s per core), far above LLM token rates.

## 4. Known limitations
- Only `delta.content` strings are redacted; `tool_calls`, `reasoning` or multi-part content arrays are passed through.
- Regex PII detection is heuristic: no names/addresses/phones, no non-US ID formats, obfuscated forms ("john dot doe at example dot com", spelled-out digits) are not caught, and a Luhn-valid number that is not a card is redacted (about 1 in 10 random 16-digit numbers).
- Cards split by unusual separators or groupings (e.g. `4111 11111111 1111`) are not matched.
- Lines from the upstream are buffered per SSE line (`aiter_lines`), so a single enormous line is held in memory.
- No auth, rate limiting or retries here (that is Task 4); one upstream, no failover.

## 5. Likely follow-up questions
1. **How do you catch PII split across chunks without buffering the whole response?** Hold back only the tail that could still become a match (a "viable prefix" regex), capped at the longest possible match, emit the rest. Memory is O(longest match), not O(response); a test streams 5 MB and asserts the buffer never exceeds that bound.
2. **What does the hold-back do to TTFT?** It delays the trailing word or number, not a fixed window, so the first token is normally emitted as soon as it arrives. The benchmark shows ~10 ms added at p50, mostly the proxy hop.
3. **How do you know the streaming output equals redacting the whole text?** Both paths use the same scanner, and a property test with random texts and random chunk sizes compares them and checks no planted PII appears in any emitted chunk (plus a 20k-case fuzz run).
4. **What happens if the upstream dies mid-stream or the client leaves?** Upstream failure: drop the held tail (it might be half an email), send a standard error event, close. Client disconnect: the generator's `finally` closes the upstream response so no work leaks; tested.
5. **Why Luhn, and what are the limits of regex redaction?** Luhn removes most false positives on random digit strings. Regex cannot catch semantic or obfuscated PII; for production I would layer an NER model or a DLP service, redact in both directions (prompt and response), and log detections as metrics without the raw values.
