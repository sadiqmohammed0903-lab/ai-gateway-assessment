"""HTTP model providers and the error taxonomy the router reasons about."""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("task4.providers")


class ProviderUnavailable(Exception):
    """Transient failure (429, 408, 5xx, timeout, connection error, garbage). Safe to fail over."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProviderRejected(Exception):
    """The provider refused the request itself (other 4xx). A backup would refuse it too."""

    def __init__(self, status: int) -> None:
        super().__init__(f"provider rejected request with status {status}")
        self.status = status


@dataclass(frozen=True)
class CompletionRequest:
    prompt: str
    max_tokens: int
    model: str | None = None


@dataclass(frozen=True)
class CompletionResult:
    text: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None
    provider: str


class HttpProvider:
    def __init__(self, name: str, url: str, client: httpx.AsyncClient, api_key: str = "") -> None:
        self.name = name
        self._url = url
        self._client = client
        self._api_key = api_key

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload: dict[str, Any] = {"prompt": request.prompt, "max_tokens": request.max_tokens}
        if request.model:
            payload["model"] = request.model
        try:
            resp = await self._client.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ProviderUnavailable("timeout") from None
        except httpx.HTTPError as exc:
            logger.warning(
                "provider=%s transport error: %s: %s", self.name, type(exc).__name__, exc
            )
            raise ProviderUnavailable("connection_error") from None

        if resp.status_code in (408, 429) or resp.status_code >= 500:
            # Raw upstream text goes to the log only, never to the client.
            logger.warning(
                "provider=%s status=%s body=%r", self.name, resp.status_code, resp.text[:500]
            )
            reason = "rate_limited" if resp.status_code == 429 else "server_error"
            raise ProviderUnavailable(reason)
        if resp.status_code >= 400:
            logger.warning(
                "provider=%s rejected request status=%s body=%r",
                self.name,
                resp.status_code,
                resp.text[:500],
            )
            raise ProviderRejected(resp.status_code)
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> CompletionResult:
        try:
            data = resp.json()
            choice = data["choices"][0]
            text = str(choice["text"])
            usage = data.get("usage") or {}
            prompt_tokens = _optional_int(usage.get("prompt_tokens"))
            completion_tokens = _optional_int(usage.get("completion_tokens"))
            return CompletionResult(
                text,
                str(choice.get("finish_reason") or "stop"),
                prompt_tokens,
                completion_tokens,
                self.name,
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            logger.warning(
                "provider=%s returned an unparseable body=%r", self.name, resp.text[:500]
            )
            raise ProviderUnavailable("bad_response") from None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
