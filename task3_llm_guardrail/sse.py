"""SSE framing helpers and the glue between OpenAI-style chunks and the redactor."""

import json
from collections.abc import Iterator
from typing import Any

from task3_llm_guardrail.redactor import StreamRedactor

DONE = "[DONE]"


def parse_data_line(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, or None for comments/other fields."""
    if not line.startswith("data:"):
        return None
    return line[5:].removeprefix(" ")


def format_event(payload: dict[str, Any] | str) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"data: {body}\n\n"


class ChunkRedactor:
    """Redacts ``delta.content`` of streamed chat-completion chunks, one redactor per choice."""

    def __init__(self) -> None:
        self._redactors: dict[int, StreamRedactor] = {}
        self._template: dict[str, Any] = {}

    def _for(self, index: int) -> StreamRedactor:
        return self._redactors.setdefault(index, StreamRedactor())

    @property
    def buffered(self) -> int:
        return sum(r.buffered for r in self._redactors.values())

    def transform(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """Return the chunk to emit, or None when it carries nothing to forward yet."""
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return chunk
        self._template = {k: v for k, v in chunk.items() if k not in ("choices", "usage")}

        keep: list[dict[str, Any]] = []
        for choice in choices:
            if not isinstance(choice, dict):
                keep.append(choice)
                continue
            redactor = self._for(int(choice.get("index", 0)))
            delta = choice.get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            finished = choice.get("finish_reason") is not None

            safe = redactor.feed(content) if isinstance(content, str) else ""
            if finished:
                safe += redactor.flush()
            if isinstance(delta, dict) and (isinstance(content, str) or safe):
                delta["content"] = safe
            only_empty_content = (
                isinstance(delta, dict) and set(delta) <= {"content"} and not delta.get("content")
            )
            if not finished and only_empty_content and isinstance(content, str):
                continue  # everything is still held back
            keep.append(choice)

        if not keep:
            return {**chunk, "choices": []} if chunk.get("usage") else None
        return {**chunk, "choices": keep}

    def flush_chunks(self) -> Iterator[dict[str, Any]]:
        """Chunks carrying whatever each choice is still holding back."""
        for index, redactor in self._redactors.items():
            text = redactor.flush()
            if text:
                choice = {"index": index, "delta": {"content": text}, "finish_reason": None}
                yield {**self._template, "choices": [choice]}
