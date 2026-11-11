"""Mock completion providers whose behaviour can be switched per test or at runtime.

Modes: normal | full_usage (reports usage == max_tokens) | slow | rate_limited | error |
garbage | bad_request.  Switch at runtime with POST /_mode {"mode": "slow"}.
"""

import asyncio
import math
import os
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

MODES = {"normal", "full_usage", "slow", "rate_limited", "error", "garbage", "bad_request"}
FAKE_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/internal/provider/app.py", line 88, in handle\n'
    "    row = db.execute('SELECT * FROM secrets')\n"
    "psycopg2.OperationalError: could not connect to server at http://10.0.0.7:5432/internal-db"
)


def create_mock_provider(name: str, mode: str = "normal", slow_seconds: float = 10.0) -> FastAPI:
    app = FastAPI(title=f"mock-provider-{name}")
    app.state.mode = mode
    app.state.calls = 0
    app.state.cancelled = 0
    app.state.last_prompt = ""

    @app.post("/_mode")
    async def set_mode(request: Request) -> dict[str, str]:
        new_mode = (await request.json()).get("mode", "")
        if new_mode not in MODES:
            raise ValueError(f"unknown mode {new_mode!r}")
        app.state.mode = new_mode
        return {"mode": new_mode}

    @app.post("/v1/completions", response_model=None)
    async def completions(request: Request) -> Response:
        body: dict[str, Any] = await request.json()
        app.state.calls += 1
        app.state.last_prompt = str(body.get("prompt", ""))
        current = app.state.mode
        if current == "slow":
            try:
                await asyncio.sleep(slow_seconds)
            except asyncio.CancelledError:
                app.state.cancelled += 1
                raise
        if current == "rate_limited":
            return JSONResponse({"error": {"message": "slow down"}}, status_code=429)
        if current == "error":
            return Response(FAKE_TRACEBACK, status_code=500, media_type="text/plain")
        if current == "garbage":
            return Response("<html>definitely not json</html>", media_type="text/html")
        if current == "bad_request":
            return JSONResponse({"error": {"message": "invalid prompt"}}, status_code=400)

        max_tokens = int(body.get("max_tokens", 16))
        completion = max_tokens if current == "full_usage" else min(max_tokens, 12)
        prompt_tokens = max(1, math.ceil(len(app.state.last_prompt) / 4))
        return JSONResponse(
            {
                "id": f"cmpl-{name}",
                "object": "text_completion",
                "choices": [
                    {
                        "index": 0,
                        "text": f"[{name}] hello from the mock model",
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion,
                    "total_tokens": prompt_tokens + completion,
                },
            }
        )

    return app


primary_app = create_mock_provider("primary", os.environ.get("PRIMARY_MODE", "normal"))
secondary_app = create_mock_provider("secondary", os.environ.get("SECONDARY_MODE", "normal"))
