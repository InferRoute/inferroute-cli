"""Local decision recorder — the privacy-local corpus of the user's model
choices and how they turned out.

Design: shared-docs/inferroute/local-decision-recorder-spec.md

Three append-only, daily-rotated JSONL event kinds:
  - choice  : a model was selected for a turn (the spine / the label)
  - outcome : what happened when that turn ran (joined by `ref` = choice id)
  - signal  : an explicit human satisfaction signal (switch / redo / rating)

Difficulty, episodes, regret, convergence are NOT recorded — they are views
computed offline over this log. We record raw signals, never verdicts.

Privacy
-------
Everything stays under `base_dir` on the user's machine. Level:
  - off      : nothing written
  - metadata : events only — hashes, counts, model ids; NO prompt text, NO blobs
  - full     : also a content-addressed blob store of raw payloads (prompt text,
               responses) so the corpus can train richer models later

Storage layout
--------------
  <base>/events/events-YYYY-MM-DD.jsonl     append-only event stream
  <base>/blobs/<aa>/<sha256>.gz             content-addressed, store-once (full)
  <base>/derived/                           offline-computed features (later)

Because Claude Code re-sends the whole conversation every turn, consecutive
turns share almost all message blocks — content addressing dedups them, so the
marginal cost of a turn is ~the novel block, not the whole context.

Fail-soft
---------
NEVER raises into the request path. All writes are best-effort and buffered;
on any error we drop the record and bump a counter.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("inferroute_local.recorder")

_VALID_LEVELS = ("off", "metadata", "full")


def _now() -> tuple[float, str]:
    t = time.time()
    return t, datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _block_bytes(block) -> bytes:
    """Deterministic serialization of a message/content block for hashing."""
    try:
        return json.dumps(block, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", "ignore"
        )
    except Exception:
        return repr(block).encode("utf-8", "ignore")


def _context_chars(messages: list) -> int:
    total = 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                for k in ("text", "content", "input"):
                    v = b.get(k)
                    if isinstance(v, str):
                        total += len(v)
                    elif isinstance(v, (dict, list)):
                        try:
                            total += len(json.dumps(v))
                        except Exception:
                            pass
    return total


def _last_user_block(messages: list):
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return m
    return None


class Recorder:
    """Append-only event recorder + content-addressed blob store. One per daemon."""

    def __init__(
        self,
        base_dir: Path,
        *,
        level: str = "metadata",
        ttl_days: int = 90,
        blob_cap_bytes: int = 65536,
        flush_every_n: int = 16,
        flush_every_s: float = 30.0,
    ):
        self.base_dir = Path(base_dir)
        self.level = level if level in _VALID_LEVELS else "metadata"
        self.ttl_days = ttl_days
        self.blob_cap_bytes = max(1024, blob_cap_bytes)
        self.flush_every_n = flush_every_n
        self.flush_every_s = flush_every_s

        self.events_dir = self.base_dir / "events"
        self.blobs_dir = self.base_dir / "blobs"

        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._dropped = 0
        # Per-session last chosen model → lets us label provenance cheaply
        # (first sight = explicit, same = sticky, changed = switch).
        self._session_model: dict[str, str] = {}

        if self.enabled:
            try:
                self.events_dir.mkdir(parents=True, exist_ok=True)
                if self.level == "full":
                    self.blobs_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"recorder dir unwritable ({e}); disabling")
                self.level = "off"

    @property
    def enabled(self) -> bool:
        return self.level != "off"

    @property
    def store_blobs(self) -> bool:
        return self.level == "full"

    @property
    def dropped(self) -> int:
        return self._dropped

    # ----- public API ------------------------------------------------------

    def record_choice(self, *, body: dict, headers: dict) -> Optional[str]:
        """Record a model selection. Returns the turn id (to join the outcome),
        or None if disabled / on error."""
        if not self.enabled:
            return None
        try:
            turn_id = uuid.uuid4().hex
            ts, iso = _now()
            session_id = self._session_id(body, headers)
            messages = body.get("messages") or []
            chosen = str(body.get("model") or "")
            provenance = self._provenance(session_id, chosen)

            block_hashes = [self._block(m) for m in messages]
            last_user = _last_user_block(messages)
            new_user_hash = self._block(last_user) if last_user is not None else None
            cmd_hash, has_cmd = self._claude_md(body.get("system"))
            tools = [
                t.get("name")
                for t in (body.get("tools") or [])
                if isinstance(t, dict) and t.get("name")
            ]

            self._emit(
                {
                    "schema_version": 1,
                    "kind": "choice",
                    "id": turn_id,
                    "session_id": session_id,
                    "ts": ts,
                    "iso": iso,
                    "chosen_model": chosen,
                    "provenance": provenance,
                    "message_count": len(messages),
                    "context_chars": _context_chars(messages),
                    "has_claude_md": has_cmd,
                    "claude_md_hash": cmd_hash,
                    "tool_names": tools,
                    "turn_block_hashes": block_hashes,
                    "new_user_block_hash": new_user_hash,
                    "stream": bool(body.get("stream")),
                }
            )
            return turn_id
        except Exception as e:
            self._dropped += 1
            logger.debug(f"record_choice skipped ({e})")
            return None

    def record_outcome(
        self,
        *,
        turn_id: Optional[str],
        session_id: str,
        status: int,
        ttft_ms: Optional[float],
        total_ms: float,
        usage: dict,
        stop_reason: Optional[str],
        served_model: str,
        error_kind: Optional[str] = None,
        response_bytes: Optional[bytes] = None,
    ) -> None:
        """Record what happened when a choice's turn ran. Best-effort."""
        if not self.enabled or turn_id is None:
            return
        try:
            ts, iso = _now()
            resp_hash = None
            if response_bytes is not None and self.store_blobs:
                resp_hash = self._store(response_bytes)
            usage = usage or {}
            self._emit(
                {
                    "schema_version": 1,
                    "kind": "outcome",
                    "id": uuid.uuid4().hex,
                    "ref": turn_id,
                    "session_id": session_id,
                    "ts": ts,
                    "iso": iso,
                    "served_model": served_model,
                    "http_status": status,
                    "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                    "total_ms": round(total_ms, 1),
                    "tokens_in": usage.get("input_tokens"),
                    "tokens_out": usage.get("output_tokens"),
                    "cache_read_tokens": usage.get("cache_read_input_tokens"),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
                    "stop_reason": stop_reason,
                    "error_kind": error_kind,
                    "response_block_hash": resp_hash,
                }
            )
        except Exception as e:
            self._dropped += 1
            logger.debug(f"record_outcome skipped ({e})")

    def record_signal(
        self,
        *,
        session_id: str,
        type: str,
        from_model: Optional[str] = None,
        to_model: Optional[str] = None,
        ref: Optional[str] = None,
        rating=None,
    ) -> None:
        """Record an explicit human satisfaction signal (switch/redo/rating)."""
        if not self.enabled:
            return
        try:
            ts, iso = _now()
            self._emit(
                {
                    "schema_version": 1,
                    "kind": "signal",
                    "id": uuid.uuid4().hex,
                    "session_id": session_id,
                    "ts": ts,
                    "iso": iso,
                    "type": type,
                    "from_model": from_model,
                    "to_model": to_model,
                    "ref": ref,
                    "rating": rating,
                }
            )
        except Exception as e:
            self._dropped += 1
            logger.debug(f"record_signal skipped ({e})")

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._flush_locked()

    def gc(self) -> None:
        """Delete raw blobs older than ttl_days. Events are kept indefinitely
        (tiny). Best-effort; safe to call at startup."""
        if self.ttl_days <= 0 or not self.blobs_dir.exists():
            return
        cutoff = time.time() - self.ttl_days * 86400
        try:
            for shard in self.blobs_dir.iterdir():
                if not shard.is_dir():
                    continue
                for blob in shard.iterdir():
                    try:
                        if blob.stat().st_mtime < cutoff:
                            blob.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    # ----- internals -------------------------------------------------------

    def _session_id(self, body: dict, headers: dict) -> str:
        sid = (headers or {}).get("x-inferroute-session") or (headers or {}).get(
            "x-inferroute-session-id"
        )
        if sid:
            return sid.strip()
        # Fallback: content-derived id (older clients without the header). Marked
        # by a prefix so offline analysis can tell them apart.
        messages = body.get("messages") or []
        first = messages[0] if messages else {}
        basis = _block_bytes(first)[:500]
        return "ch_" + _sha256(basis)[:16]

    def _provenance(self, session_id: str, chosen: str) -> str:
        last = self._session_model.get(session_id)
        self._session_model[session_id] = chosen
        if last is None:
            return "human_explicit"
        if last != chosen:
            return "human_switch"
        return "human_sticky"

    def _block(self, block) -> str:
        """Hash a message block; store it (full mode). Returns the hash."""
        data = _block_bytes(block)
        h = _sha256(data)
        if self.store_blobs:
            self._store_at(h, data)
        return h

    def _claude_md(self, system) -> tuple[Optional[str], bool]:
        if not system:
            return None, False
        if isinstance(system, list):
            text = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        else:
            text = str(system)
        # CLAUDE.md is injected into the system prompt; CC marks project context.
        has = "CLAUDE.md" in text or "[PROJECT]" in text[:400]
        h = _sha256(text.encode("utf-8", "ignore")) if text else None
        if self.store_blobs and text:
            self._store_at(h, text.encode("utf-8", "ignore"))
        return h, has

    def _store(self, data: bytes) -> str:
        h = _sha256(data)
        self._store_at(h, data)
        return h

    def _store_at(self, h: str, data: bytes) -> None:
        """Write one content-addressed blob (gzip), store-once. Oversize blobs
        are truncated head+tail so a single huge tool-result can't bloat the
        store — the hash still reflects the full content for dedup/reference."""
        try:
            shard = self.blobs_dir / h[:2]
            path = shard / f"{h}.gz"
            if path.exists():  # dedup — already stored
                return
            shard.mkdir(parents=True, exist_ok=True)
            if len(data) > self.blob_cap_bytes:
                half = self.blob_cap_bytes // 2
                data = (
                    data[:half]
                    + f"...<truncated {len(data)} bytes>...".encode()
                    + data[-half:]
                )
            tmp = path.with_suffix(".gz.tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(data)
            tmp.replace(path)
        except Exception:
            pass  # blob store is best-effort; the event still records the hash

    def _emit(self, event: dict) -> None:
        try:
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            self._dropped += 1
            logger.debug(f"event serialize failed ({e})")
            return
        with self._lock:
            self._buf.append(line)
            if (
                len(self._buf) >= self.flush_every_n
                or time.monotonic() - self._last_flush >= self.flush_every_s
            ):
                self._flush_locked()

    def _current_path(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.events_dir / f"events-{today}.jsonl"

    def _flush_locked(self) -> None:
        if not self._buf:
            return
        chunk = "\n".join(self._buf) + "\n"
        self._buf.clear()
        self._last_flush = time.monotonic()
        try:
            with self._current_path().open("a", encoding="utf-8") as f:
                f.write(chunk)
        except Exception as e:
            self._dropped += chunk.count("\n")
            if self._dropped <= 5 or self._dropped % 100 == 0:
                logger.warning(f"event write failed ({e}); dropped={self._dropped}")
