"""Token counting with tiktoken, falling back to a conservative estimate when offline."""

import logging
import math

logger = logging.getLogger("task4.tokens")

ENCODING_NAME = "cl100k_base"


class TokenCounter:
    def __init__(self, encoding_name: str = ENCODING_NAME) -> None:
        self._encoding_name = encoding_name
        self._encoding: object | None = None
        self._loaded = False

    def _load(self) -> None:
        self._loaded = True
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(self._encoding_name)
        except Exception as exc:  # tiktoken downloads its vocabulary on first use
            logger.warning(
                "tiktoken unavailable (%s); using a len/3 estimate that over-counts",
                type(exc).__name__,
            )

    @property
    def uses_tiktoken(self) -> bool:
        if not self._loaded:
            self._load()
        return self._encoding is not None

    def count(self, text: str) -> int:
        if not self._loaded:
            self._load()
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))  # type: ignore[attr-defined]
        return math.ceil(len(text) / 3)  # biased high on purpose: never under-reserve
