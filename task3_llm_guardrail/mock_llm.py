"""Mock OpenAI-compatible streaming LLM. Deliberately splits PII across chunk boundaries."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DEFAULT_CHUNKS = [
    "Sure! You can reach me at john.do",
    "e@exam",
    "ple.com or on my cell. My SSN is 123-4",
    "5-6789",
    " and my card is 4111 ",
    "1111 11",
    "11 1111",
    ". Thanks and have a great day!",
]


def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-llm",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def create_mock_llm(chunks: list[str] | None = None, delay_ms: float | None = None) -> FastAPI:
    app = FastAPI(title="mock-llm")
    script = chunks if chunks is not None else DEFAULT_CHUNKS
    delay = (
        delay_ms if delay_ms is not None else float(os.environ.get("MOCK_LLM_DELAY_MS", "0"))
    ) / 1000
    app.state.cancelled = 0

    async def events() -> AsyncIterator[str]:
        try:
            yield _chunk({"role": "assistant", "content": ""})
            for piece in script:
                if delay:
                    await asyncio.sleep(delay)
                yield _chunk({"content": piece})
            yield _chunk({}, finish="stop")
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            app.state.cancelled += 1
            raise

    @app.post("/v1/chat/completions", response_model=None)
    async def completions(request: Request) -> StreamingResponse | JSONResponse:
        body = await request.json()
        if not body.get("stream", False):
            text = "".join(script)
            message = {"role": "assistant", "content": text}
            return JSONResponse(
                {"id": "chatcmpl-mock", "choices": [{"index": 0, "message": message}]}
            )
        return StreamingResponse(events(), media_type="text/event-stream")

    return app


app = create_mock_llm()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9101, log_level="warning")
