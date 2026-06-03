"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Local daemon
    host: str = "127.0.0.1"
    port: int = 5005

    # Where to forward Anthropic requests (user's own credentials pass-through)
    anthropic_base_url: str = "https://api.anthropic.com"

    # inferroute-server endpoints
    inferroute_server_url: str = "https://api.inferroute.ai"
    inferroute_api_key: str = ""

    # Optional fast-path shortcut: skip the server round-trip when context is
    # very full (likely approaching compaction). Magic number — tune or remove.
    fast_path_context_ratio: float = 0.75

    # Compaction-only signals that force Anthropic locally. Keep these PRECISE —
    # Claude Code's default system prompt often mentions generic phrases like
    # "context window", so we only match phrases that are unambiguously
    # mid-compaction or summarization signals.
    compaction_keywords: list[str] = field(default_factory=lambda: [
        "Compacting conversation",
        "Claude is compacting",
        "<compacting>",
        "automatically compacted",
        # Observed 2026-05-28 while reviewing CC session data: the system
        # prompt CC sets when injecting an auto-compact continuation summary.
        # Without this entry, the daemon would server-route compacted turns
        # to MiniMax — the model would receive a "session continuation"
        # summary as a user message and be unable to act on it coherently.
        "Conversation compacted",
    ])
    planning_keywords: list[str] = field(default_factory=lambda: [
        "architect",
        "design the system",
        "plan the implementation",
        "think through",
        "ultrathink",
        "high level plan",
    ])

    # Backend model IDs forwarded to inferroute-server.
    # Tier 2 is now MiniMax M2.5 only (Kimi/GLM retired 2026-05-28).
    # During testing this is the Chutes-served id; owned-B200 path swaps via env var.
    minimax_model: str = "MiniMaxAI/MiniMax-M2.5-TEE"
    # Retained as deprecated aliases so old INFERROUTE_KIMI_MODEL / INFERROUTE_GLM_MODEL
    # env vars don't crash existing deployments; new code paths ignore them.
    kimi_model: str = ""
    glm_model: str = ""

    # Timeout for /inferroute/route metadata call (ms → seconds)
    route_timeout_s: float = 1.0

    # Local routing classifier (Phase 1 of the v0 stability/routing plan).
    # When enabled and a model dir is available, the daemon runs a local
    # ONNX classifier (~15-30ms on CPU) to decide tier-routing, replacing
    # the server-side /inferroute/route round-trip. Falls back to the
    # server call when the model isn't installed or fails to load.
    # See classifier_v2.py + shared-docs/inferroute/stability-and-routing.md
    classifier_v2_enabled: bool = True
    # Optional explicit path to the classifier-v0 directory (must contain
    # onnx/model.onnx + tokenizer + calibration.json). Leave empty to let
    # the loader search the default locations:
    #   ~/.inferroute/models/classifier-v0
    #   ~/.local/share/inferroute/models/classifier-v0
    #   /opt/inferroute/models/classifier-v0
    classifier_model_dir: str = ""

    # On-demand model fetch (Phase 4).
    # Defaults to the InferRoute/inferroute-cli GitHub Releases "latest"
    # asset — so `ir add local-routing` works out of the box. Override via
    # INFERROUTE_CLASSIFIER_BOOTSTRAP_URL for staging or custom manifests.
    # See bootstrap.py for the manifest schema and docs/RELEASING.md for
    # how the bundle is built. Always fail-soft: a bad URL or sha mismatch
    # leaves the daemon running on the legacy server route.
    classifier_bootstrap_url: str = (
        "https://github.com/InferRoute/inferroute-cli/releases/latest/download/"
        "classifier-v0-manifest.json"
    )

    # Per-turn decision logging (Phase 5).
    # Writes one JSONL record per routed turn to ~/.inferroute/logs/.
    # PRIVACY MODEL — default is metadata-only (NO prompt content). Users
    # who want to share training data back set INFERROUTE_LOG_TRAINING=1,
    # which additionally records the assembled classifier input verbatim
    # into the SAME file. Either way, logs stay on the user's machine
    # unless they explicitly upload them. See decision_log.py for the
    # full record schema.
    decision_log_enabled: bool = True
    decision_log_include_text: bool = False
    # Absolute path to the log dir. Empty → use ~/.inferroute/logs/
    decision_log_dir: str = ""

    # ── Local decision recorder ──────────────────────────────────────────
    # The privacy-local corpus of the user's model choices + how they turned
    # out. Replaces the old routing decision-log. Everything stays under
    # record_dir on the user's machine; nothing is uploaded.
    # See shared-docs/inferroute/local-decision-recorder-spec.md.
    #   level "off"      → record nothing
    #   level "metadata" → choice/outcome/signal events only (no prompt text)
    #   level "full"     → also a content-addressed blob store of raw payloads
    # Default ON (metadata).
    record_level: str = "metadata"
    record_dir: str = ""              # empty → ~/.inferroute
    record_ttl_days: int = 90         # raw blob GC age; 0 = keep forever
    record_blob_cap_bytes: int = 65536  # per-blob head+tail cap (oversize trim)

    # Session-aware router (Phase 2 of stability/routing).
    # commit_threshold — on the first turn of a session, if the classifier's
    #   max probability is below this, we route provisionally to minimax_ok
    #   and don't lock the session into a tier. Subsequent turns re-evaluate.
    # session_ttl_seconds — how long a session entry survives without activity
    #   before being treated as a new session. Default 2h matches typical
    #   CC session timeouts.
    router_commit_threshold: float = 0.6
    router_session_ttl_seconds: float = 2 * 3600

    # Compaction-on-upgrade (Phase 3 of stability/routing).
    # When the router upgrades a session mid-conversation (e.g. minimax_ok →
    # frontier), we ask MiniMax to summarize the prior turns and replace the
    # message history with that summary so the upgraded tier doesn't inherit
    # a partial conversation written by a weaker model. Fails open: any error
    # forwards the original history unchanged.
    # See compactor.py + shared-docs/inferroute/stability-and-routing.md.
    compactor_enabled: bool = True
    # Skip compaction on very short sessions (nothing meaningful to summarize).
    # 4 = at least one prior user/assistant pair plus the new user turn.
    compactor_min_messages: int = 4
    # Hard cap on MiniMax summary length. Tokens, not chars. The summary is
    # injected as a single user message, so this directly governs handoff
    # context size at the upgraded tier.
    compactor_max_summary_tokens: int = 600
    # Block at most this many seconds waiting for the summary. The user is
    # waiting for the upgraded turn to start; we must not stall indefinitely.
    compactor_timeout_s: float = 15.0

    # Tool-output compression (Headroom).
    # RETIRED on the daemon (2026-06-01): compression moved SERVER-SIDE into
    # cc-proxy-prod, where it runs in cache-safe stateful mode (prefix-freeze +
    # kompress). The daemon's old stateless token-mode path rewrote history and
    # busted the backend prefix cache *before* the request reached the server —
    # so it MUST stay off, or it defeats the server-side cache safety. Default
    # off; set INFERROUTE_COMPRESS=1 only for experiments on Anthropic-direct
    # turns (which bypass the server and are otherwise uncompressed).
    # See shared-docs/inferroute + headroom-cache-mode-design memory.
    compress_enabled: bool = False
    # Blocks smaller than this many tokens are left untouched (Headroom's
    # min_tokens_to_compress). Tune up to be more conservative.
    compress_min_tokens: int = 250
    # Optional Headroom kompress ML model id (e.g. "chopratejas/kompress-base").
    # Left empty for v1 (heuristic-only base, ~2% real-traffic reduction). Setting
    # it unlocks the 60-95% ratios from the strategy doc but requires the
    # headroom-ai[ml] extra (torch + ~600MB model download). One-flag upgrade.
    compress_kompress_model: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=os.environ.get("INFERROUTE_HOST", "127.0.0.1"),
            port=int(os.environ.get("INFERROUTE_PORT", "5005")),
            anthropic_base_url=os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com"),
            inferroute_server_url=os.environ.get("INFERROUTE_SERVER_URL", "https://api.inferroute.ai"),
            inferroute_api_key=os.environ.get("INFERROUTE_API_KEY", ""),
            minimax_model=os.environ.get("INFERROUTE_MINIMAX_MODEL", "MiniMaxAI/MiniMax-M2.5-TEE"),
            kimi_model=os.environ.get("INFERROUTE_KIMI_MODEL", ""),
            glm_model=os.environ.get("INFERROUTE_GLM_MODEL", ""),
            compress_enabled=_env_bool("INFERROUTE_COMPRESS", False),
            compress_min_tokens=int(os.environ.get("INFERROUTE_COMPRESS_MIN_TOKENS", "250")),
            compress_kompress_model=os.environ.get("INFERROUTE_COMPRESS_KOMPRESS_MODEL", ""),
            classifier_v2_enabled=_env_bool("INFERROUTE_CLASSIFIER_V2", True),
            classifier_model_dir=os.environ.get("INFERROUTE_CLASSIFIER_DIR", ""),
            classifier_bootstrap_url=os.environ.get(
                "INFERROUTE_CLASSIFIER_BOOTSTRAP_URL", ""
            ),
            decision_log_enabled=_env_bool("INFERROUTE_LOG_DECISIONS", True),
            decision_log_include_text=_env_bool("INFERROUTE_LOG_TRAINING", False),
            decision_log_dir=os.environ.get("INFERROUTE_LOG_DIR", ""),
            record_level=_record_level_default(),
            record_dir=os.environ.get(
                "INFERROUTE_RECORD_DIR", os.environ.get("INFERROUTE_LOG_DIR", "")
            ),
            record_ttl_days=int(os.environ.get("INFERROUTE_RECORD_TTL_DAYS", "90")),
            record_blob_cap_bytes=int(
                os.environ.get("INFERROUTE_RECORD_BLOB_CAP_BYTES", "65536")
            ),
            router_commit_threshold=float(
                os.environ.get("INFERROUTE_ROUTER_COMMIT_THRESHOLD", "0.6")
            ),
            router_session_ttl_seconds=float(
                os.environ.get("INFERROUTE_ROUTER_TTL_SECONDS", str(2 * 3600))
            ),
            compactor_enabled=_env_bool("INFERROUTE_COMPACTOR", True),
            compactor_min_messages=int(
                os.environ.get("INFERROUTE_COMPACTOR_MIN_MESSAGES", "4")
            ),
            compactor_max_summary_tokens=int(
                os.environ.get("INFERROUTE_COMPACTOR_MAX_TOKENS", "600")
            ),
            compactor_timeout_s=float(
                os.environ.get("INFERROUTE_COMPACTOR_TIMEOUT_S", "15.0")
            ),
        )


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var. Accepts 1/0, true/false, yes/no (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _record_level_default() -> str:
    """Resolve the recorder level, honoring the legacy decision-log env vars so
    existing installs keep working:
      INFERROUTE_RECORD_LEVEL wins if set (off|metadata|full);
      else INFERROUTE_LOG_TRAINING=1 → full;
      else INFERROUTE_LOG_DECISIONS=0 → off;
      else → metadata (default ON)."""
    explicit = os.environ.get("INFERROUTE_RECORD_LEVEL")
    if explicit:
        lvl = explicit.strip().lower()
        return lvl if lvl in {"off", "metadata", "full"} else "metadata"
    if _env_bool("INFERROUTE_LOG_TRAINING", False):
        return "full"
    if not _env_bool("INFERROUTE_LOG_DECISIONS", True):
        return "off"
    return "metadata"
