"""Benchmark TTFT and total latency: direct-to-mock vs through the guardrail gateway.

Run: python -m task3_llm_guardrail.bench_ttft --runs 50 --delay-ms 20
"""

import argparse
import asyncio
import json
import statistics
import threading
import time

import httpx
import uvicorn

from task3_llm_guardrail.app import Settings, create_app
from task3_llm_guardrail.mock_llm import create_mock_llm

MOCK_PORT, GATEWAY_PORT = 9201, 9200
BODY = {"model": "mock", "messages": [{"role": "user", "content": "hi"}], "stream": True}


class ServerThread(threading.Thread):
    def __init__(self, app: object, port: int) -> None:
        super().__init__(daemon=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")  # type: ignore[arg-type]
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def start_and_wait(self) -> None:
        self.start()
        while not self.server.started:
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=5)


async def measure(client: httpx.AsyncClient, url: str) -> tuple[float, float]:
    """Return (TTFT, total) in milliseconds; TTFT is the first event with non-empty content."""
    start = time.perf_counter()
    ttft: float | None = None
    async with client.stream("POST", url, json=BODY) as resp:
        async for line in resp.aiter_lines():
            if ttft is None and line.startswith("data: {"):
                choices = json.loads(line[6:]).get("choices") or [{}]
                if choices[0].get("delta", {}).get("content"):
                    ttft = (time.perf_counter() - start) * 1000
    total = (time.perf_counter() - start) * 1000
    return ttft if ttft is not None else total, total


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


async def run(runs: int) -> dict[str, dict[str, list[float]]]:
    targets = {
        "direct": f"http://127.0.0.1:{MOCK_PORT}/v1/chat/completions",
        "gateway": f"http://127.0.0.1:{GATEWAY_PORT}/v1/chat/completions",
    }
    results: dict[str, dict[str, list[float]]] = {n: {"ttft": [], "total": []} for n in targets}
    async with httpx.AsyncClient(timeout=30) as client:
        for url in targets.values():  # warm-up
            await measure(client, url)
        for _ in range(runs):
            for name, url in targets.items():  # interleave to cancel drift
                ttft, total = await measure(client, url)
                results[name]["ttft"].append(ttft)
                results[name]["total"].append(total)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--delay-ms", type=float, default=20.0, help="mock delay between chunks")
    args = parser.parse_args()

    mock = ServerThread(create_mock_llm(delay_ms=args.delay_ms), MOCK_PORT)
    gateway_app = create_app(
        Settings(upstream_url=f"http://127.0.0.1:{MOCK_PORT}/v1/chat/completions")
    )
    gateway = ServerThread(gateway_app, GATEWAY_PORT)
    mock.start_and_wait()
    gateway.start_and_wait()
    try:
        results = asyncio.run(run(args.runs))
    finally:
        gateway.stop()
        mock.stop()

    print(f"runs={args.runs} chunk_delay={args.delay_ms}ms (8 content chunks)")
    print(f"{'':8} {'metric':6} {'p50 ms':>9} {'p95 ms':>9} {'mean ms':>9}")
    for name, metrics in results.items():
        for metric, values in metrics.items():
            print(
                f"{name:8} {metric:6} {percentile(values, 50):9.1f} "
                f"{percentile(values, 95):9.1f} {statistics.mean(values):9.1f}"
            )
    for metric in ("ttft", "total"):
        delta = percentile(results["gateway"][metric], 50) - percentile(
            results["direct"][metric], 50
        )
        print(f"added p50 {metric}: {delta:+.1f} ms")


if __name__ == "__main__":
    main()
