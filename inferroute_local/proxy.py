"""Pass-through recorder proxy.

The local daemon does NOT route, classify, or decide anything. The user picks a
model per session (`ir --model …` / `ir choose`); the daemon's only jobs are:

  1. record the choice + how the turn went, locally and privately (recorder.py)
  2. forward the request upstream to the inferroute cloud, which serves the
     pinned model and applies its own backend fallbacks

There is no local router, classifier, or compactor anymore — see
shared-docs/inferroute/local-decision-recorder-spec.md. Cloud-side behavior
(fallbacks, e2ee, x402) is unchanged and lives in inferroute-server.

Everything here is fail-soft for recording and fail-clear for forwarding: a
recording error never touches the request; an upstream error returns a clean
Anthropic-shaped error to Claude Code.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from .config import Config
from .recorder import Recorder

logger = logging.getLogger("inferroute_local")


class InferrouteProxy:
    def __init__(self, config: Config):
        self.config = config
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))

        base_dir = (
            Path(config.record_dir)
            if config.record_dir
            else Path.home() / ".inferroute"
        )
        self.recorder = Recorder(
            base_dir,
            level=config.record_level,
            ttl_days=config.record_ttl_days,
            blob_cap_bytes=config.record_blob_cap_bytes,
        )
        # Prune expired raw blobs once at startup (best-effort, cheap).
        self.recorder.gc()

    async def close(self):
        await self._client.aclose()
        try:
            self.recorder.flush()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Public entry point
    # -------------------------------------------------------------------------

    async def handle(
        self,
        body: dict,
        request_headers: dict[str, str],
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        """Record the choice, forward upstream, record the outcome. Returns
        (status, headers, body_stream)."""
        session_id = (request_headers.get("x-inferroute-session") or "").strip()
        turn_id = self.recorder.record_choice(body=body, headers=request_headers)
        chosen_model = str(body.get("model") or "")
        streaming = bool(body.get("stream"))
        start = time.monotonic()

        try:
            status, resp_headers, stream = await self._forward(body, request_headers)
        except httpx.HTTPError as e:
            logger.warning(f"upstream unreachable ({e})")
            self.recorder.record_outcome(
                turn_id=turn_id, session_id=session_id, status=502,
                ttft_ms=None, total_ms=(time.monotonic() - start) * 1000,
                usage={}, stop_reason=None, served_model=chosen_model,
                error_kind=type(e).__name__,
            )
            return self._inject_error(
                streaming,
                "inferroute-local: upstream is unreachable. Check your connection "
                "or run `ir status`.",
            )

        wrapped = self._recording_stream(
            stream, turn_id=turn_id, session_id=session_id,
            streaming=streaming, chosen_model=chosen_model,
            status=status, start=start,
        )
        return status, resp_headers, wrapped

    # -------------------------------------------------------------------------
    # Forward upstream (to the inferroute cloud, which serves the pinned model)
    # -------------------------------------------------------------------------

    async def _forward(
        self, body: dict, request_headers: dict[str, str]
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        url = f"{self.config.inferroute_server_url}/v1/messages"
        headers = _forward_headers(request_headers)
        return await self._stream(url, body, headers)

    # -------------------------------------------------------------------------
    # Outcome-recording stream wrapper
    # -------------------------------------------------------------------------

    async def _recording_stream(
        self,
        stream: AsyncIterator[bytes],
        *,
        turn_id: Optional[str],
        session_id: str,
        streaming: bool,
        chosen_model: str,
        status: int,
        start: float,
    ) -> AsyncIterator[bytes]:
        """Yield the upstream bytes unchanged while observing first-byte time,
        total time, usage, stop_reason, and (full mode) a capped response blob.
        Emits exactly one outcome event when the stream is exhausted."""
        first: Optional[float] = None
        cap = self.recorder.blob_cap_bytes
        head = bytearray()
        usage: dict = {}
        stop_reason: Optional[str] = None
        served: Optional[str] = None
        linebuf = ""              # streaming SSE line accumulator
        full = bytearray() if not streaming else None  # small JSON body

        try:
            async for chunk in stream:
                if first is None:
                    first = time.monotonic()
                if len(head) < cap:
                    head.extend(chunk[: cap - len(head)])
                if streaming:
                    linebuf += chunk.decode("utf-8", "ignore")
                    parts = linebuf.split("\n")
                    linebuf = parts.pop()  # keep trailing partial line
                    for line in parts:
                        s, sv = _scan_sse_line(line, usage)
                        if s:
                            stop_reason = s
                        if sv:
                            served = sv
                elif full is not None:
                    full.extend(chunk)
                yield chunk
        finally:
            total_ms = (time.monotonic() - start) * 1000
            ttft_ms = ((first - start) * 1000) if first is not None else None
            if streaming:
                s, sv = _scan_sse_line(linebuf, usage)  # flush last partial
                if s:
                    stop_reason = s
                if sv:
                    served = sv
                blob = bytes(head)
            else:
                u, s, sv = _extract_json(bytes(full or b""))
                if u:
                    usage = u
                stop_reason = s or stop_reason
                served = sv or served
                blob = bytes((full or b"")[:cap])
            # Capture the real session cost regardless of record_level (even
            # "off") — it's a single content-free number, and it's the only thing
            # that lets the status line show the true price. The rich outcome
            # event below stays gated by record_level inside record_outcome.
            self.recorder.note_cost(session_id, usage.get("cost"))
            try:
                self.recorder.record_outcome(
                    turn_id=turn_id, session_id=session_id, status=status,
                    ttft_ms=ttft_ms, total_ms=total_ms, usage=usage,
                    stop_reason=stop_reason, served_model=served or chosen_model,
                    response_bytes=blob,
                )
            except Exception as e:
                logger.debug(f"outcome record skipped ({e})")

    # -------------------------------------------------------------------------
    # Streaming helper
    # -------------------------------------------------------------------------

    async def _stream(
        self, url: str, body: dict, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        req = self._client.build_request("POST", url, json=body, headers=headers)
        response = await self._client.send(req, stream=True)

        response_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() in {
                "content-type", "x-request-id", "anthropic-version",
                "request-id", "transfer-encoding",
            }
        }

        async def _iter():
            async for chunk in response.aiter_bytes():
                yield chunk
            await response.aclose()

        return response.status_code, response_headers, _iter()

    # -------------------------------------------------------------------------
    # Error injection
    # -------------------------------------------------------------------------

    def _inject_error(
        self, streaming: bool, message: str
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        """Return a 502 carrying a clear Anthropic-shaped error so Claude Code
        surfaces the text instead of a blank failure."""
        err = {"type": "error", "error": {"type": "api_error", "message": message}}
        if streaming:
            sse = f"event: error\ndata: {json.dumps(err)}\n\n".encode()

            async def _gen():
                yield sse

            return 502, {"content-type": "text/event-stream"}, _gen()

        body_bytes = json.dumps(err).encode()

        async def _gen_json():
            yield body_bytes

        return 502, {"content-type": "application/json"}, _gen_json()


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing helpers (Anthropic Messages format)
# ─────────────────────────────────────────────────────────────────────────────

def _scan_sse_line(line: str, usage: dict) -> tuple[Optional[str], Optional[str]]:
    """Parse one SSE line; merge token usage into `usage`. Returns
    (stop_reason, served_model) found on this line, or (None, None)."""
    line = line.strip()
    if not line.startswith("data:"):
        return None, None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None, None
    try:
        obj = json.loads(payload)
    except Exception:
        return None, None
    return _apply_obj(obj, usage)


def _merge_usage(dst: dict, src) -> None:
    """Merge an Anthropic ``usage`` object into ``dst``.

    Keeps integer token counters AND the inferroute-added ``usage.cost`` (USD,
    a float — the server-computed per-request cost; see
    shared-docs/inferroute/goose-real-cost-display-spec.md) plus ``cost_currency``.
    Bools are skipped (``bool`` is an ``int`` subclass) and everything else is
    ignored, so a stray field can't poison the record.
    """
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            dst[k] = v
        elif k == "cost" and isinstance(v, float):
            dst[k] = v
        elif k == "cost_currency" and isinstance(v, str):
            dst[k] = v


def _apply_obj(obj: dict, usage: dict) -> tuple[Optional[str], Optional[str]]:
    t = obj.get("type")
    stop = None
    served = None
    if t == "message_start":
        msg = obj.get("message") or {}
        served = msg.get("model")
        _merge_usage(usage, msg.get("usage"))
    elif t == "message_delta":
        stop = (obj.get("delta") or {}).get("stop_reason")
        # inferroute emits the final usage.cost on message_delta (output_tokens
        # are known only here), so this is where cost is captured for streams.
        _merge_usage(usage, obj.get("usage"))
    return stop, served


def _extract_json(raw: bytes) -> tuple[dict, Optional[str], Optional[str]]:
    """Parse a non-streaming Anthropic response body."""
    usage: dict = {}
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return usage, None, None
    if not isinstance(obj, dict):
        return usage, None, None
    _merge_usage(usage, obj.get("usage"))
    return usage, obj.get("stop_reason"), obj.get("model")


def _forward_headers(incoming: dict[str, str]) -> dict[str, str]:
    """Headers to forward upstream: auth, anthropic version/beta, content-type,
    and the per-launch session id so the cloud can tag usage for the dashboard."""
    keep = {
        "authorization", "x-api-key", "anthropic-version", "anthropic-beta",
        "content-type", "x-inferroute-session", "x-inferroute-session-id",
    }
    headers = {k.lower(): v for k, v in incoming.items() if k.lower() in keep}
    headers.setdefault("content-type", "application/json")
    headers.setdefault("anthropic-version", "2023-06-01")
    return headers
