"""Primary/secondary routing with a hard primary timeout and a small circuit breaker."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

from task4_model_router.providers import (
    CompletionRequest,
    CompletionResult,
    ProviderRejected,
    ProviderUnavailable,
)

logger = logging.getLogger("task4.router")


class Provider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...


class AllProvidersFailed(Exception):
    pass


class CircuitBreaker:
    """Skip the primary for `cooldown` seconds after `threshold` consecutive failures.

    After the cooldown calls are allowed again (a probe); one success closes the breaker and a
    failure re-opens it. Concurrent probes in the half-open state are tolerated.
    """

    def __init__(
        self,
        threshold: int = 3,
        cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        return self._opened_at is None or self._clock() - self._opened_at >= self._cooldown

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        return not self.allow()


class ModelRouter:
    def __init__(
        self,
        primary: Provider,
        secondary: Provider,
        primary_timeout: float = 3.0,
        secondary_timeout: float = 10.0,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._primary_timeout = primary_timeout
        self._secondary_timeout = secondary_timeout
        self._breaker = breaker

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if self._breaker is None or self._breaker.allow():
            try:
                result = await self._call(self._primary, request, self._primary_timeout)
            except ProviderRejected:
                raise  # a client error is not an availability signal: no failover, no breaker hit
            except (ProviderUnavailable, TimeoutError) as exc:
                if self._breaker:
                    self._breaker.record_failure()
                logger.warning("primary failed (%s); failing over", _describe(exc))
            else:
                if self._breaker:
                    self._breaker.record_success()
                return result
        else:
            logger.warning("primary circuit open; going straight to secondary")

        try:
            return await self._call(self._secondary, request, self._secondary_timeout)
        except (ProviderUnavailable, TimeoutError) as exc:
            logger.error("secondary failed too (%s)", _describe(exc))
            raise AllProvidersFailed from None

    @staticmethod
    async def _call(
        provider: Provider, request: CompletionRequest, timeout: float
    ) -> CompletionResult:
        # asyncio.timeout cancels the in-flight call (closing its connection) when it fires.
        async with asyncio.timeout(timeout):
            return await provider.complete(request)


def _describe(exc: Exception) -> str:
    return exc.reason if isinstance(exc, ProviderUnavailable) else "timeout"
