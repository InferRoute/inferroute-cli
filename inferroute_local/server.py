"""FastAPI server — listens on port 5005, intercepts Claude Code traffic."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .proxy import InferrouteProxy

logger = logging.getLogger("inferroute_local")


def create_app(config: Config) -> FastAPI:
    proxy = InferrouteProxy(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from . import stats as _stats
        _stats.load_persisted()  # restore cumulative compression savings
        logger.info(
            f"inferroute-local listening on {config.host}:{config.port} "
            f"→ {config.inferroute_server_url} "
            f"(recording: {config.record_level})"
        )
        yield
        await proxy.close()
        logger.info("inferroute-local stopped")

    app = FastAPI(title="inferroute-local", lifespan=lifespan)

    @app.post("/v1/messages")
    async def messages(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
            )

        headers = dict(request.headers)
        status, resp_headers, stream = await proxy.handle(body, headers)

        if body.get("stream", False):
            return StreamingResponse(
                stream,
                status_code=status,
                headers=resp_headers,
                media_type="text/event-stream",
            )
        else:
            # Collect non-streaming response
            chunks = []
            async for chunk in stream:
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode(errors="replace")
            return JSONResponse(content=parsed, status_code=status, headers=resp_headers)

    @app.post("/inferroute/signal")
    async def signal(request: Request):
        """Control surface: the CLI/UX posts explicit human signals here
        (model switch, redo-on-stronger, rating). Recorded locally; never
        forwarded anywhere. Body: {session_id, type, from_model?, to_model?,
        ref?, rating?}."""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            proxy.recorder.record_signal(
                session_id=str(payload.get("session_id") or ""),
                type=str(payload.get("type") or "unknown"),
                from_model=payload.get("from_model"),
                to_model=payload.get("to_model"),
                ref=payload.get("ref"),
                rating=payload.get("rating"),
            )
        except Exception:
            pass
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "inferroute-local"}

    @app.get("/stats")
    async def stats(reset: int = 0):
        from . import stats as _stats
        if reset:
            _stats.reset()
            return {"reset": True}
        return _stats.snapshot()

    @app.get("/v1/models")
    async def models():
        """Pass-through: Claude Code polls this to discover models."""
        return {
            "object": "list",
            "data": [
                {"id": "claude-opus-4-5-20251101", "object": "model"},
                {"id": "claude-sonnet-4-5-20251101", "object": "model"},
                {"id": "claude-haiku-4-5-20251001", "object": "model"},
            ],
        }

    return app
