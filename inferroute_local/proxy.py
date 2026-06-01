"""Core proxy logic: receives Claude Code requests, routes them.

Routing (post-2026-06-01 architecture; see
shared-docs/inferroute/stability-and-routing.md for the canonical design):

  Layer 1 — Fast-path (0ms): compaction signals / very-high context_ratio
            → direct to Anthropic with user's own credentials
  Layer 2 — Local classifier (~15-30ms, runs on user's CPU)
            → 3-class probs {minimax_ok, middle_tier, frontier}
            → for Phase 1 we collapse to binary: frontier → anthropic,
              else → minimax via inferroute-server
            → if classifier isn't installed / fails to load, fall back to
              the legacy server-side /inferroute/route call (Layer 3)
  Layer 3 — Legacy server route (50-80ms): fallback for when the local
            classifier isn't available. Eventually retired.

Phase 2+ (not yet built): adds session stickiness, switch thresholds,
deferred commitment, compaction-on-upgrade — all in a separate router.py.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from . import stats as _stats
from .classifier import RequestMeta, classify, is_fast_path_anthropic
from .classifier_v2 import RoutingClassifier, ClassifierResult, assemble_text
from .compactor import maybe_compact, should_compact
from .compression import Compressor, Savings
from .config import Config
from .decision_log import DecisionLogger, build_record
from .router import RouterDecision, TierRouter, compute_session_key, tier_to_backend

logger = logging.getLogger("inferroute_local")


class InferrouteProxy:
    def __init__(self, config: Config):
        self.config = config
        self.compressor = Compressor(config)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
        self._route_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.route_timeout_s)
        )
        # Optional one-shot model fetch on first startup. No-op when the URL
        # is empty (default) or a model already exists at the target path.
        # Fail-soft: any error here is logged and the daemon proceeds without
        # the classifier (legacy server route handles routing in that case).
        if config.classifier_bootstrap_url and not config.classifier_model_dir:
            from .bootstrap import maybe_bootstrap_classifier
            from pathlib import Path
            maybe_bootstrap_classifier(
                Path.home() / ".inferroute" / "models" / "classifier-v0",
                config.classifier_bootstrap_url,
            )
        # Local classifier: lazy-loads ONNX on first classify call. If the
        # model isn't installed, .available stays False and we fall back to
        # the legacy server /inferroute/route endpoint.
        self._classifier = RoutingClassifier(
            model_dir=config.classifier_model_dir or None,
        )
        # Session-aware router wraps the classifier with stickiness, asymmetric
        # switch thresholds, deferred commitment, and hard-rule overrides.
        # See router.py + shared-docs/inferroute/stability-and-routing.md.
        self._router = TierRouter(
            classifier=self._classifier,
            commit_threshold=config.router_commit_threshold,
            session_ttl_seconds=config.router_session_ttl_seconds,
        )
        # Per-turn decision logger (Phase 5). Default-on, metadata-only.
        # Users wanting to share training data back set INFERROUTE_LOG_TRAINING=1.
        # All writes are best-effort; logger never raises into the request path.
        from pathlib import Path as _Path
        log_dir = (
            _Path(config.decision_log_dir)
            if config.decision_log_dir
            else _Path.home() / ".inferroute" / "logs"
        )
        self._decision_log = DecisionLogger(
            log_dir=log_dir,
            enabled=config.decision_log_enabled,
            include_text=config.decision_log_include_text,
        )

    async def close(self):
        await self._client.aclose()
        await self._route_client.aclose()
        # Flush any buffered decision-log records before exit. Best-effort.
        try:
            self._decision_log.flush()
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
        """Route a /v1/messages request. Returns (status, headers, body_stream)."""
        user_agent = request_headers.get("user-agent", "")

        # [1] Tool-output compression (in-flight, applies to BOTH destinations).
        # Fail-open: on any issue the original body is returned unchanged.
        body, savings = self.compressor.compress_body(body)
        if savings.applied:
            logger.debug(
                f"compressed {savings.tokens_before}→{savings.tokens_after} tokens "
                f"(saved {savings.tokens_saved}, {savings.ratio:.1%})"
            )

        meta = classify(body, self.config, user_agent)

        # Determine system text for fast-path check
        system = body.get("system", "")
        system_text = " ".join(b.get("text", "") for b in system if isinstance(b, dict)) if isinstance(system, list) else (system or "")

        if is_fast_path_anthropic(meta, self.config, system_text):
            logger.debug(f"fast-path→anthropic ratio={meta.context_ratio:.2f}")
            self._record("anthropic_fast", "compaction_or_high_context", meta, savings)
            self._log_decision(
                body=body, decision=None, classifier_result=None,
                tier="frontier", reason="compaction_or_high_context",
                backend="anthropic", fast_path=True, compacted=False,
            )
            return await self._forward_anthropic(body, request_headers)

        # Honor explicit model pick. `ir --model NAME` sends a specific model
        # in the request body; the user has already made a routing choice and
        # the classifier has no business overriding it. Only run the classifier
        # when model is the auto-routing sentinel ("multi-model"), or when
        # there's no model field at all (some clients omit it).
        # Backend selection in this branch: claude-* names → user's own
        # Anthropic creds (direct pass-through); anything else → server.
        explicit_model = (body.get("model") or "").strip()
        if explicit_model and explicit_model != "multi-model":
            if explicit_model.startswith("claude-"):
                backend = "anthropic"
                reason = f"explicit_model:{explicit_model}"
            else:
                backend = "minimax"  # server-side leg handles MiniMax/Kimi/GLM/etc.
                reason = f"explicit_model:{explicit_model}"
            logger.debug(f"explicit-model→{backend} model={explicit_model}")
            self._record(
                "anthropic_explicit" if backend == "anthropic" else "minimax_explicit",
                reason, meta, savings,
            )
            self._log_decision(
                body=body, decision=None, classifier_result=None,
                tier=("frontier" if backend == "anthropic" else "minimax_ok"),
                reason=reason, backend=backend, fast_path=True, compacted=False,
            )
            if backend == "anthropic":
                return await self._forward_anthropic(body, request_headers)
            # Server route — the body's `model` field already names the right
            # backend, no need for model_id substitution.
            try:
                status, hdrs, stream = await self._forward_inferroute_server(
                    body, request_headers, "minimax", reason, savings,
                )
                if status >= 500:
                    async for _ in stream:
                        pass
                    return await self._forward_anthropic(body, request_headers)
                return status, hdrs, stream
            except httpx.HTTPError as e:
                logger.warning(f"server unreachable on explicit-model leg ({e}), failing open→anthropic")
                return await self._forward_anthropic(body, request_headers)

        # Local classifier first; fall back to legacy server route on miss.
        backend, reason, route_label, decision = await self._decide_route(
            body, meta, request_headers
        )
        logger.debug(
            f"route→{backend} reason={reason} "
            f"ratio={meta.context_ratio:.2f} files={meta.file_count_estimate}"
        )

        # Compaction on tier-upgrade switches: summarize prior history via
        # MiniMax so the upgraded backend doesn't inherit a partial conversation
        # written by a weaker model. No-op on downgrades and non-switches.
        compacted = False
        if decision is not None and should_compact(decision, body, self.config):
            body, compact_status = await maybe_compact(
                body, decision, self._client, self.config
            )
            if compact_status == "compacted":
                compacted = True
                logger.info(
                    f"compacted history for upgrade "
                    f"{decision.previous_tier}→{decision.tier}"
                )
                reason = f"{reason}+compacted"

        self._record(route_label, reason or "unknown", meta, savings)
        # Per-turn decision log (Phase 5 — feedstock for v1 retraining).
        self._log_decision(
            body=body, decision=decision,
            classifier_result=decision.classifier_result if decision else None,
            tier=(decision.tier if decision else ("frontier" if backend == "anthropic" else "minimax_ok")),
            reason=reason or "unknown",
            backend=backend, fast_path=False, compacted=compacted,
        )

        if backend == "anthropic":
            return await self._forward_anthropic(body, request_headers)

        # Try inferroute-server (Kimi/GLM). Fail-open to Anthropic on connect
        # errors or 5xx so Claude Code keeps working when our server is down.
        try:
            status, resp_headers, stream = await self._forward_inferroute_server(
                body, request_headers, backend, reason, savings
            )
            if status >= 500:
                logger.warning(
                    f"inferroute-server returned {status} on backend={backend}, "
                    "fail-open→anthropic"
                )
                # Drain and discard the failed response body
                async for _ in stream:
                    pass
                return await self._forward_anthropic(body, request_headers)
            if status == 401:
                # inferroute token issue — inject friendly error
                return self._inject_error(
                    body.get("stream", False),
                    "Your inferroute session has expired. Run: inferroute login",
                )
            return status, resp_headers, stream
        except httpx.HTTPError as e:
            logger.warning(f"inferroute-server unreachable ({e}), fail-open→anthropic")
            return await self._forward_anthropic(body, request_headers)

    # -------------------------------------------------------------------------
    # Stats helper
    # -------------------------------------------------------------------------

    def _log_decision(
        self,
        *,
        body: dict,
        decision: RouterDecision | None,
        classifier_result: ClassifierResult | None,
        tier: str,
        reason: str,
        backend: str,
        fast_path: bool,
        compacted: bool,
    ) -> None:
        """Emit one DecisionRecord. Fail-soft: any error here is swallowed —
        logging must never break a routed request."""
        try:
            text = assemble_text(body)  # cheap; same string the classifier sees
            session_key = compute_session_key(body)
            record = build_record(
                body=body,
                assembled_text=text,
                session_key=session_key,
                classifier_result=classifier_result,
                decision=decision,
                tier=tier,
                reason=reason,
                backend=backend,
                fast_path=fast_path,
                compacted=compacted,
                include_text=self.config.decision_log_include_text,
            )
            self._decision_log.emit(record)
        except Exception as e:
            logger.debug(f"decision log skipped ({e})")

    def _record(self, route: str, reason: str, meta: RequestMeta, savings: Savings) -> None:
        _stats.record(
            route, reason,
            model_in=meta.model,
            file_count=meta.file_count_estimate,
            context_ratio=meta.context_ratio,
            tokens_before=savings.tokens_before if savings.applied else 0,
            tokens_after=savings.tokens_after if savings.applied else 0,
            tokens_saved=savings.tokens_saved if savings.applied else 0,
        )

    # -------------------------------------------------------------------------
    # Anthropic forward (pass-through credentials)
    # -------------------------------------------------------------------------

    async def _forward_anthropic(
        self, body: dict, request_headers: dict[str, str]
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        url = f"{self.config.anthropic_base_url}/v1/messages"
        headers = _forward_headers(request_headers, extra={
            "anthropic-version": request_headers.get("anthropic-version", "2023-06-01"),
        })
        status, resp_headers, stream = await self._stream(url, body, headers)
        if status == 401:
            # Drain the upstream body so the connection is released
            async for _ in stream:
                pass
            return self._inject_error(
                body.get("stream", False),
                "Your Anthropic session has expired. Run /login in Claude Code to re-authenticate.",
            )
        return status, resp_headers, stream

    # -------------------------------------------------------------------------
    # Error injection
    # -------------------------------------------------------------------------

    def _inject_error(
        self, streaming: bool, message: str
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        """Return a 401 response carrying a clear error message.

        For streaming requests we emit a single SSE error event so Claude Code
        surfaces the text. For non-streaming we return JSON in the standard
        Anthropic error envelope.
        """
        if streaming:
            payload = {"type": "error", "error": {"type": "authentication_error", "message": message}}
            sse = (
                f"event: error\ndata: {json.dumps(payload)}\n\n".encode()
            )

            async def _gen():
                yield sse

            return 401, {"content-type": "text/event-stream"}, _gen()

        body_bytes = json.dumps(
            {"type": "error", "error": {"type": "authentication_error", "message": message}}
        ).encode()

        async def _gen_json():
            yield body_bytes

        return 401, {"content-type": "application/json"}, _gen_json()

    # -------------------------------------------------------------------------
    # Kimi/GLM forward (through inferroute-server which holds our credentials)
    # -------------------------------------------------------------------------

    async def _forward_inferroute_server(
        self, body: dict, request_headers: dict[str, str], backend: str,
        reason: str = "", savings: Savings | None = None,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        url = f"{self.config.inferroute_server_url}/v1/messages"

        # Substitute model so inferroute-server routes to the right backend.
        # During testing these are Chutes model IDs; swap via env vars for production.
        import copy
        body = copy.deepcopy(body)
        if backend == "minimax":
            body["model"] = self.config.minimax_model
        elif backend == "kimi" and self.config.kimi_model:
            # Legacy path — kept for migration period only.
            body["model"] = self.config.kimi_model
        elif backend == "glm" and self.config.glm_model:
            # Legacy path — kept for migration period only.
            body["model"] = self.config.glm_model

        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.config.inferroute_api_key}",
            "anthropic-version": request_headers.get("anthropic-version", "2023-06-01"),
            # Forward user-agent so Kimi Code whitelist check can pass through later
            "x-forwarded-user-agent": request_headers.get("user-agent", ""),
            # Tell the server which inferroute rule fired — surfaces in the
            # sidecar log + claude-devtools UI as the routing "reason".
            "x-inferroute-reason": reason or "unknown",
            "x-inferroute-backend": backend,
        }
        # Surface compression savings to inferroute-server so it can persist them
        # into usage_records (→ rendered in the inferroute.ai session view).
        if savings is not None and savings.applied:
            headers["x-inferroute-compress-saved"] = str(savings.tokens_saved)
            headers["x-inferroute-compress-before"] = str(savings.tokens_before)
            headers["x-inferroute-compress-after"] = str(savings.tokens_after)
        return await self._stream(url, body, headers)

    # -------------------------------------------------------------------------
    # Routing decision
    # -------------------------------------------------------------------------

    async def _decide_route(
        self, body: dict, meta: RequestMeta, request_headers: dict[str, str]
    ) -> tuple[str, str, str, RouterDecision | None]:
        """Decide which backend handles this request.

        Goes through the session-aware TierRouter (Phase 2):

          - Hard rules first (ultrathink keyword, x-inferroute-tier header)
          - Classifier inference
          - Stickiness + asymmetric switch thresholds
          - Deferred commitment for low-confidence first turns

        Falls back to the legacy server /inferroute/route call only when the
        classifier itself is unavailable (model not installed, load failed).
        Returns (backend, reason, stats_label, decision). `decision` is None
        on the legacy-fallback path (no upgrade signal available there, so
        compaction is skipped).
        """
        if self.config.classifier_v2_enabled:
            decision = self._router.route(body, request_headers)
            if decision is not None:
                backend = tier_to_backend(decision.tier)
                label = "anthropic_clf" if backend == "anthropic" else "minimax_clf"
                return backend, decision.reason, label, decision

        # Layer 3 fallback: legacy server-side route
        backend, reason = await self._get_routing_decision(meta)
        label = "anthropic_server" if backend == "anthropic" else backend
        return backend, reason, label, None

    async def _get_routing_decision(self, meta: RequestMeta) -> tuple[str, str]:
        """Call /inferroute/route with metadata. Fail-open to 'anthropic'.

        Returns (backend, reason). `reason` is the server-provided rule name
        ('simple_task', 'high_context', etc.) or 'route_unavailable' on failure.
        """
        try:
            headers = {}
            if self.config.inferroute_api_key:
                headers["authorization"] = f"Bearer {self.config.inferroute_api_key}"

            resp = await self._route_client.post(
                f"{self.config.inferroute_server_url}/inferroute/route",
                json={
                    "context_ratio": meta.context_ratio,
                    "has_planning_keywords": meta.has_planning_keywords,
                    "file_count_estimate": meta.file_count_estimate,
                    "is_simple_task": meta.is_simple_task,
                    "model": meta.model,
                    "has_frontend_signals": meta.has_frontend_signals,
                    "has_backend_signals": meta.has_backend_signals,
                    "tools_count": meta.tools_count,
                    "message_count": meta.message_count,
                },
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("backend", "anthropic"), data.get("reason", "unknown")
        except Exception as e:
            logger.warning(f"route call failed ({e}), fail-open→anthropic")
        return "anthropic", "route_unavailable"

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


def _forward_headers(
    incoming: dict[str, str],
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build headers to forward upstream, keeping auth and version headers."""
    keep = {
        "authorization", "x-api-key", "anthropic-version",
        "anthropic-beta", "content-type",
    }
    headers = {k.lower(): v for k, v in incoming.items() if k.lower() in keep}
    headers.setdefault("content-type", "application/json")
    if extra:
        headers.update({k.lower(): v for k, v in extra.items()})
    return headers
