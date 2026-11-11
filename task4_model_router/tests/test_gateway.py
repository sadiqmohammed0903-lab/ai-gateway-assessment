import asyncio
import logging
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from common.logging import LOG_FORMAT, RequestIdFilter
from task4_model_router.mock_providers import FAKE_TRACEBACK
from task4_model_router.tests.conftest import KEY_A, KEY_B, FakeClock, make_env

ERROR_KEYS = {"type", "code", "message", "request_id"}
LEAKS = ["Traceback", "psycopg2", "10.0.0.7", "internal-db", "/srv/internal", "SELECT * FROM"]


def assert_standard_error(resp: httpx.Response, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    error = resp.json()["error"]
    assert set(resp.json()) == {"error"} and set(error) == ERROR_KEYS
    assert error["code"] == code
    assert error["request_id"] and error["request_id"] == resp.headers["x-request-id"]
    return error


def assert_no_leaks(resp: httpx.Response) -> None:
    blob = resp.text + str(dict(resp.headers))
    for leak in LEAKS + ["http://primary", "http://secondary", "gateway.db"]:
        assert leak not in blob, leak


# ---------------- happy path, auth, validation ----------------


async def test_success_served_by_primary_with_provider_header(tmp_path: Path) -> None:
    async with make_env(tmp_path) as env:
        resp = await env.post("hello world", max_tokens=100)
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "primary"
    body = resp.json()
    assert body["choices"][0]["text"].startswith("[primary]")
    assert (
        body["usage"]["total_tokens"]
        == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )
    assert "primary/v1" not in str(dict(resp.headers))


async def test_unused_reserved_tokens_are_released_after_the_response(tmp_path: Path) -> None:
    async with make_env(tmp_path) as env:
        resp = await env.post("a" * 40, max_tokens=1_000)  # reserves 1,040
        usage = resp.json()["usage"]
        rows = env.rows("tenant-a")
    assert len(rows) == 1 and rows[0][2] == "settled"
    assert rows[0][1] == usage["total_tokens"] and rows[0][1] < 100  # not 1,040


async def test_invalid_or_missing_api_key_is_401(tmp_path: Path) -> None:
    async with make_env(tmp_path) as env:
        for key in (None, "", "wrong-key"):
            resp = await env.post(key=key)
            assert_standard_error(resp, 401, "invalid_api_key")
        assert env.primary.state.calls == 0 and env.rows() == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": 123},
        {"prompt": "x", "max_tokens": 0},
        {"prompt": "x", "max_tokens": -5},
        {"prompt": "x", "max_tokens": "many"},
        {"prompt": "x", "max_tokens": 10**9},
    ],
)
async def test_validation_errors_use_the_standard_schema(tmp_path: Path, payload: dict) -> None:
    async with make_env(tmp_path) as env:
        resp = await env.client.post("/v1/completions", json=payload, headers={"X-API-Key": KEY_A})
        assert_standard_error(resp, 400, "invalid_request")
        assert "detail" not in resp.json() and env.rows() == []


async def test_malformed_json_and_unknown_routes_use_the_standard_schema(tmp_path: Path) -> None:
    async with make_env(tmp_path) as env:
        bad = await env.client.post(
            "/v1/completions",
            content=b"{nope",
            headers={"X-API-Key": KEY_A, "content-type": "application/json"},
        )
        assert_standard_error(bad, 400, "invalid_request")
        assert_standard_error(await env.client.get("/nope"), 404, "not_found")
        assert_standard_error(await env.client.get("/v1/completions"), 405, "method_not_allowed")


# ---------------- rate limiting over HTTP ----------------


async def test_over_the_limit_gets_429_with_retry_after_and_window_slides(tmp_path: Path) -> None:
    async with make_env(tmp_path, tokens_per_minute=1_000, primary_mode="full_usage") as env:
        first = await env.post("a" * 100, max_tokens=500)  # 600 tokens, kept at full usage
        assert first.status_code == 200
        env.clock.advance(10)
        second = await env.post("b" * 100, max_tokens=500)  # needs 600, only 400 left
        error = assert_standard_error(second, 429, "rate_limit_exceeded")
        assert error["type"] == "rate_limit_error"
        assert second.headers["retry-after"] == "50"
        calls_before = env.primary.state.calls

        env.clock.advance(51)
        third = await env.post("c" * 100, max_tokens=500)
        assert third.status_code == 200
        assert env.primary.state.calls == calls_before + 1  # 429 never reached a provider
        assert len(env.rows("tenant-a")) == 1, "expired + rejected rows must not accumulate"


async def test_request_bigger_than_the_budget_is_400_not_429(tmp_path: Path) -> None:
    async with make_env(tmp_path, tokens_per_minute=1_000) as env:
        resp = await env.post("a" * 10, max_tokens=5_000)
        assert_standard_error(resp, 400, "request_too_large")
        assert "retry-after" not in resp.headers


async def test_one_tenant_hitting_the_limit_does_not_affect_another(tmp_path: Path) -> None:
    async with make_env(tmp_path, tokens_per_minute=1_000, primary_mode="full_usage") as env:
        assert (await env.post("a" * 100, 900, key=KEY_A)).status_code == 200
        assert (await env.post("a" * 100, 900, key=KEY_A)).status_code == 429
        assert (await env.post("b" * 100, 900, key=KEY_B)).status_code == 200


async def test_fifty_parallel_requests_never_admit_more_than_the_limit(tmp_path: Path) -> None:
    async with make_env(tmp_path, primary_mode="full_usage") as env:
        responses = await asyncio.gather(*(env.post("x" * 10, max_tokens=1_990) for _ in range(50)))
        ok = [r for r in responses if r.status_code == 200]
        limited = [r for r in responses if r.status_code == 429]
        assert len(ok) + len(limited) == 50 and len(ok) == 25
        total = sum(tokens for _, tokens, _ in env.rows("tenant-a"))
        assert total <= 50_000
        assert all(r.headers["retry-after"].isdigit() for r in limited)


# ---------------- failover ----------------


@pytest.mark.parametrize("mode", ["rate_limited", "error", "garbage"])
async def test_primary_failure_modes_fail_over_to_secondary(tmp_path: Path, mode: str) -> None:
    async with make_env(tmp_path, primary_mode=mode) as env:
        resp = await env.post()
        assert resp.status_code == 200, resp.text
        assert resp.headers["x-gateway-provider"] == "secondary"
        assert resp.json()["choices"][0]["text"].startswith("[secondary]")
        assert env.primary.state.calls == 1 and env.secondary.state.calls == 1
        assert_no_leaks(resp)


async def test_primary_connection_error_fails_over(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused by 10.0.0.7:8443")

    async with make_env(tmp_path, primary_transport=httpx.MockTransport(refuse)) as env:
        resp = await env.post()
        assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "secondary"
        assert_no_leaks(resp)


async def test_slow_primary_times_out_at_3s_and_secondary_serves(tmp_path: Path) -> None:
    async with make_env(tmp_path, primary_mode="slow") as env:  # default timeout is 3000 ms
        tasks_before = asyncio.all_tasks()
        start = time.perf_counter()
        resp = await env.post()
        elapsed = time.perf_counter() - start
        await asyncio.sleep(0)  # let cancellation settle
        leaked = asyncio.all_tasks() - tasks_before
    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "secondary"
    assert 3.0 <= elapsed < 4.5, f"expected ~3.x s, got {elapsed:.2f}s"
    assert env.primary.state.cancelled == 1, "slow primary call was not cancelled"
    assert not leaked, f"leaked tasks: {leaked}"


async def test_timeout_is_configurable_and_both_slow_gives_503(tmp_path: Path) -> None:
    async with make_env(
        tmp_path,
        primary_mode="slow",
        secondary_mode="slow",
        primary_timeout=0.2,
        secondary_timeout=0.2,
    ) as env:
        start = time.perf_counter()
        resp = await env.post()
        elapsed = time.perf_counter() - start
        assert_standard_error(resp, 503, "all_providers_unavailable")
        assert 0.4 <= elapsed < 1.5
        assert env.primary.state.cancelled == 1 and env.secondary.state.cancelled == 1
        assert env.rows("tenant-a")[0][1:] == (0, "released")


async def test_client_error_from_primary_does_not_fail_over(tmp_path: Path) -> None:
    async with make_env(tmp_path, primary_mode="bad_request") as env:
        resp = await env.post()
        assert_standard_error(resp, 400, "provider_rejected_request")
        assert env.secondary.state.calls == 0
        assert env.rows("tenant-a")[0][1:] == (0, "released")
        assert "invalid prompt" not in resp.text


@pytest.mark.parametrize("mode", ["rate_limited", "error", "garbage"])
async def test_both_providers_down_gives_sanitised_503_and_releases_tokens(
    tmp_path: Path, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    async with make_env(tmp_path, primary_mode=mode, secondary_mode=mode) as env:
        resp = await env.post("a" * 10, max_tokens=500)
        assert_standard_error(resp, 503, "all_providers_unavailable")
        assert resp.headers["retry-after"] == "5"
        assert_no_leaks(resp)
        assert env.rows("tenant-a")[0][1:] == (0, "released")
        assert env.primary.state.calls == 1 and env.secondary.state.calls == 1


# ---------------- sanitisation ----------------


async def test_provider_error_details_are_logged_but_never_returned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    # Same filter and format that configure_logging installs in production.
    caplog.handler.addFilter(RequestIdFilter())
    caplog.handler.setFormatter(logging.Formatter(LOG_FORMAT))
    async with make_env(tmp_path, primary_mode="error", secondary_mode="error") as env:
        resp = await env.post()
    assert_no_leaks(resp)
    assert "Traceback" in FAKE_TRACEBACK
    assert "10.0.0.7" in caplog.text and "psycopg2" in caplog.text
    request_id = resp.json()["error"]["request_id"]
    provider_lines = [ln for ln in caplog.text.splitlines() if "provider=" in ln]
    assert provider_lines and all(f"[{request_id}]" in ln for ln in provider_lines)


async def test_internal_exception_is_sanitised_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("SELECT * FROM api_keys WHERE secret='hunter2' at /srv/internal/db.py")

    caplog.set_level(logging.ERROR)
    async with make_env(tmp_path, raise_app_exceptions=False) as env:
        env.app.state.router.complete = boom
        resp = await env.post()
        assert_standard_error(resp, 500, "internal_error")
        assert resp.json()["error"]["message"] == "Internal gateway error"
        assert "hunter2" not in resp.text and "api_keys" not in resp.text
        assert env.rows("tenant-a")[0][1:] == (0, "released"), "reservation must be released"
    assert "hunter2" in caplog.text and "Traceback" in caplog.text


async def test_database_error_is_sanitised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked: /var/lib/gateway/prod.db")

    caplog.set_level(logging.ERROR)
    async with make_env(tmp_path, raise_app_exceptions=False) as env:
        env.app.state.limiter.reserve = locked
        resp = await env.post()
        assert_standard_error(resp, 500, "internal_error")
        assert "locked" not in resp.text and "/var/lib" not in resp.text
    assert "prod.db" in caplog.text


# ---------------- persistence and breaker ----------------


async def test_sqlite_state_survives_an_app_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    async with make_env(
        tmp_path, clock=clock, tokens_per_minute=1_000, primary_mode="full_usage"
    ) as env:
        assert (await env.post("a" * 100, 500)).status_code == 200  # 600 tokens
    async with make_env(
        tmp_path, clock=clock, tokens_per_minute=1_000, primary_mode="full_usage"
    ) as env:
        assert len(env.rows("tenant-a")) == 1
        resp = await env.post("a" * 100, 500)
        assert_standard_error(resp, 429, "rate_limit_exceeded")
        assert (await env.post("a" * 10, 100, key=KEY_B)).status_code == 200  # keys survived too


async def test_circuit_breaker_skips_a_failing_primary_then_probes_after_cooldown(
    tmp_path: Path,
) -> None:
    async with make_env(
        tmp_path, primary_mode="error", breaker_threshold=2, breaker_cooldown=30.0
    ) as env:
        for _ in range(2):
            assert (await env.post()).headers["x-gateway-provider"] == "secondary"
        assert env.primary.state.calls == 2

        skipped = await env.post()
        assert skipped.headers["x-gateway-provider"] == "secondary"
        assert env.primary.state.calls == 2, "open breaker must not call the primary"

        env.clock.advance(31)
        env.primary.state.mode = "normal"
        probe = await env.post()
        assert probe.headers["x-gateway-provider"] == "primary" and env.primary.state.calls == 3
        assert (await env.post()).headers["x-gateway-provider"] == "primary"


async def test_token_counter_falls_back_when_tiktoken_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tiktoken

    from task4_model_router.tokens import TokenCounter

    def offline(name: str) -> None:
        raise OSError("no network")

    monkeypatch.setattr(tiktoken, "get_encoding", offline)
    counter = TokenCounter()
    assert counter.count("a" * 30) == 10 and not counter.uses_tiktoken


def test_real_tiktoken_counts_tokens_not_characters() -> None:
    from task4_model_router.tokens import TokenCounter

    counter = TokenCounter()
    text = "The quick brown fox jumps over the lazy dog."
    count = counter.count(text)
    if counter.uses_tiktoken:
        assert count == 10
    else:  # offline machine without a cached vocabulary: the estimate must over-count
        assert count >= 10
