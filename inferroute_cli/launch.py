"""Spawn `claude` with the right env + flags. Never mutates the user's shell.

Two flavours:
  - launch_through_inferroute(): inject ANTHROPIC_BASE_URL + KEY, set --model
  - launch_native_anthropic():   exec plain `claude` untouched (escape hatch)

Both always add --dangerously-skip-permissions so the agent runs without
permission prompts. We assume the user installed `ir` precisely to skip
that friction; they can still use plain `claude` themselves if they
want the prompts back.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Iterable

from .config import Credentials


_DEFAULT_FLAGS = ["--dangerously-skip-permissions"]

# There is no local router and no auto-route. The user always pins a concrete
# model. The on-device daemon, when installed+running, is a pure RECORDING
# pass-through: we route the session through it so it logs the choice + outcome
# locally, then it forwards to the cloud (which still does its own fallbacks).
# When the daemon isn't running we talk to the cloud directly.
# See shared-docs/inferroute/local-decision-recorder-spec.md.
LOCAL_DAEMON_URL = os.environ.get("INFERROUTE_LOCAL_URL", "http://localhost:5005").rstrip("/")


def _recording_daemon_url() -> str | None:
    """Return the on-device recorder daemon's URL if it's running, else None.

    Cheap 150ms reachability probe so launch never noticeably stalls. The daemon
    is installed (and local recording enabled) via `ir add`; when it's absent we
    fall back to talking to the cloud directly and simply don't record.
    """
    import socket
    from urllib.parse import urlparse

    u = urlparse(LOCAL_DAEMON_URL)
    host = u.hostname or "localhost"
    port = u.port or 80
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return LOCAL_DAEMON_URL
    except OSError:
        return None


def _session_url(api_url: str, session_id: str) -> str:
    """Build the dashboard URL for this session from the API base URL.

    The session page at <site>/session/[id] is keyed by the per-launch
    `session_id` we mint at launch and inject into every Claude Code request (via
    ANTHROPIC_CUSTOM_HEADERS). The proxy tags each usage_records row with it, so
    the link shows exactly — and only — the requests this `ir` invocation
    produced, even when several `ir` sessions run concurrently. (Older links use
    a `from-<unix_ms>` timestamp-window slug; the dashboard still accepts those.)
    """
    # api.inferroute.ai → inferroute.ai (the dashboard sits on the apex domain).
    # Works for https://api.X and http://api.X; leaves anything else alone.
    site = api_url
    for prefix in ("https://api.", "http://api."):
        if site.startswith(prefix):
            scheme = prefix.split("://", 1)[0]  # "https" or "http"
            site = scheme + "://" + site[len(prefix):]
            break
    # Note: it's `/session/...` not `/dashboard/session/...` — the
    # (dashboard) route group in inferroute-site is parenthesised and
    # therefore not part of the URL path (Next.js App Router convention).
    return f"{site.rstrip('/')}/session/{session_id}"


def _print_session_link(api_url: str, session_id: str) -> None:
    """Print a clickable URL to view this session's traffic on the dashboard.

    Also persist the URL to ~/.config/inferroute/last_session for retrieval via
    `ir status` or shell history.

    NOTE: this pre-launch print is NOT durable. Claude Code's fullscreen TUI
    (alt-screen, enabled via `/tui fullscreen` or CLAUDE_CODE_NO_FLICKER=1) hides
    everything printed before launch for the whole session, and even the inline
    renderer scrolls it off-screen as the conversation grows. The persistent copy
    of this link lives in the status line — see `_product_strip_settings_args`.
    """
    url = _session_url(api_url, session_id)

    # Persist for later retrieval (e.g. `ir status`, or user copy-paste).
    try:
        target = Path.home() / ".config" / "inferroute" / "last_session"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url + "\n")
    except OSError:
        pass

    # Print to stderr so Claude Code's TUI screen-clear (on stdout) doesn't
    # immediately wipe it. Most terminals keep stderr in scrollback.
    sys.stderr.write(
        f"\n  ➜ View this session at\n    \033[36m{url}\033[0m\n\n"
    )
    sys.stderr.flush()


def _last_model_file() -> Path:
    return Path.home() / ".config" / "inferroute" / "last_model"


def _persist_last_model(model_id: str) -> None:
    """Remember the model this launch pinned, so `ir --resume` can reuse it.

    `claude --resume` doesn't ask which model to use; to match that, `ir --resume`
    must not pop the model picker — it reuses whatever you last ran. Best-effort;
    a write failure just means `ir --resume` falls back to the picker.
    """
    if not model_id:
        return
    try:
        target = _last_model_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(model_id.strip() + "\n")
    except OSError:
        pass


def last_model() -> str | None:
    """The canonical model_id of the most recent inferroute launch, or None.

    Used by `ir --resume` / `ir --continue` to resume without the model picker.
    Returns the stored id verbatim (it's a valid `--model` value); None if nothing
    has been launched yet or the file is unreadable.
    """
    try:
        v = _last_model_file().read_text().strip()
        return v or None
    except OSError:
        return None


def _user_has_statusline() -> bool:
    """True if the user already configures a statusLine we'd otherwise clobber.

    Our injected statusLine arrives via the `--settings` CLI flag, which is a
    higher-precedence layer than user/project/local settings.json — so it would
    REPLACE any statusLine the user defined. To avoid stomping their setup we
    check the settings files they could have set one in and back off if so.
    """
    import json

    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
        Path.cwd() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.local.json",
    ]
    for p in candidates:
        try:
            if p.is_file():
                data = json.loads(p.read_text())
                if isinstance(data, dict) and data.get("statusLine"):
                    return True
        except (OSError, ValueError):
            continue
    return False


def _model_for_statusline(extra_args: list[str]) -> str | None:
    """The `--model X` value the user passed through (verbatim X), or None.

    Only used by the NATIVE path for the strip header / relaunch hint — the gate
    path already has its resolved model_id. Native passes args straight to
    claude, so X is whatever claude understands (`sonnet`, `claude-opus-4-8`, …)
    and we echo it back unchanged.
    """
    i = 0
    while i < len(extra_args):
        a = extra_args[i]
        if a == "--model" and i + 1 < len(extra_args):
            return extra_args[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
        i += 1
    return None


def _record_sessions_dir() -> Path:
    """Where the on-device recorder daemon writes per-session cost files.

    Mirrors inferroute_local.config.Config.record_dir resolution
    (INFERROUTE_RECORD_DIR → INFERROUTE_LOG_DIR → ~/.inferroute) so the status
    line reads the same path the daemon wrote.
    """
    base = os.environ.get("INFERROUTE_RECORD_DIR") or os.environ.get("INFERROUTE_LOG_DIR") or ""
    return (Path(base) if base else Path.home() / ".inferroute") / "sessions"


def _strip_command(prefix: str, cost_file: Path | None = None) -> dict:
    """A CC statusLine `command` dict that prints the strip + real session cost.

    The static strip (model · lane │ link │ ↻ hint) is known at launch, so it's a
    plain `printf` of `prefix` — dependency-free, no per-render subprocess, and it
    ignores the session JSON CC pipes on stdin.

    Cost is the REAL inferroute figure, not CC's. We deliberately do NOT use CC's
    `cost.total_cost_usd` from stdin: CC prices with its own built-in rates for the
    model id, and our routed models (kimi/glm/… via the proxy) are far cheaper than
    whatever CC assumes for a non-Anthropic id, so that number runs several times
    high — exactly wrong on a cost-savings product. Instead, the on-device recorder
    daemon captures the server-computed `usage.cost` off each response (the same
    number the dashboard bills) and keeps a per-session running total in a tiny
    local file (`<record_dir>/sessions/<session_id>.cost`, full-precision USD). The
    status line just reads that file — local, no network — and printf-formats it to
    cents. When the daemon isn't running (no file) the strip is simply the static
    two lines; cost appears once the first turn settles. The `|| true` keeps the
    command's exit status 0 even on a malformed/again-empty read, so CC still
    renders the line (it only displays stdout when the command exits 0).

    Cost lands last on the final line, so it's the first thing CC's width-clip drops.
    """
    import shlex

    command = "printf '%s' " + shlex.quote(prefix)
    if cost_file is not None:
        cf = shlex.quote(str(cost_file))
        command += (
            f"; if [ -s {cf} ]; then "
            f"printf ' │ $%.2f' \"$(cat {cf} 2>/dev/null)\" 2>/dev/null || true; fi"
        )
    return {"type": "command", "command": command}


def _product_strip_settings_args(
    prefix: str, extra_args: list[str], cost_file: Path | None = None
) -> list[str]:
    """`['--settings', <json>]` pinning the product strip to CC's status line, or [].

    Claude Code's fullscreen TUI (alt-screen — the DEFAULT in CC 2.1.170, mode
    `ant_default`) hides anything printed before launch, and the inline renderer
    eventually scrolls it away. So neither the pre-launch banner nor a post-session
    print (impossible anyway: we `execvp` claude and the ir process is replaced)
    can durably surface ir's session info. A statusLine is rendered BY claude
    INSIDE the TUI, so it survives fullscreen and stays pinned for the whole
    session in BOTH render modes.

    We inject it via `--settings` (a per-invocation layer; no temp files, no
    polluting the user's repo with a .claude/settings.json). Because that layer
    overrides the user's own statusLine, we back off when:
      * IR_NO_STATUSLINE is set (explicit opt-out),
      * the user passed their own --settings (respect it), or
      * the user already has a statusLine configured (don't clobber it).
    """
    import json

    if os.environ.get("IR_NO_STATUSLINE", "").strip().lower() in ("1", "true", "yes"):
        return []
    if any(a == "--settings" or a.startswith("--settings=") for a in extra_args):
        return []
    if _user_has_statusline():
        return []

    settings = {"statusLine": _strip_command(prefix, cost_file)}
    return ["--settings", json.dumps(settings)]


def _gate_strip_prefix(
    api_url: str, session_id: str, model_id: str, economy: bool
) -> str:
    """One-line product strip for the gate (inferroute) launch path:

        ⚡ <model> · <lane> │ <dashboard link>      (+ live cost appended)

    Single line by design: a dedicated second line for a relaunch hint
    (`↻ ir --model …`) wasn't worth the vertical space, and CC gives the
    statusLine no terminal width, so the hint can't be right-pegged on this line
    either. The model name in the header already implies how to relaunch. We
    reverse-map the resolved model_id back to the friendly short (`kimi`, not
    `moonshotai/Kimi-K2.6-TEE`) for that header; cost is appended last so it's the
    first thing CC's width-clip drops on a narrow terminal.
    """
    from . import models

    short = models.short_for_model_id(model_id) or model_id
    lane = "economy" if economy else "standard"
    url = _session_url(api_url, session_id)
    return f"⚡ {short} · {lane} │ {url}"


def _native_strip_prefix(extra_args: list[str]) -> str:
    """One-line product strip for the native (`ir anthropic`) launch path:

        ⚡ <model> · native

    No dashboard link (native deliberately doesn't route through inferroute, so
    there's no minted session) and no cost (no daemon in the path). `<model>` is
    the verbatim `--model` value the user passed, if any, else `claude`.
    """
    model = _model_for_statusline(extra_args)
    head = model or "claude"
    return f"⚡ {head} · native"


def _print_economy_banner() -> None:
    """Print a green 'economy lane' banner when this launch is tagged economy.

    Fires when IR_LANE=economy is in the environment (set by a deferred-loop gate
    snippet, or by `ir` itself in an economy context). Purely cosmetic — the actual
    discount is decided server-side at serve time. Goes to stderr so Claude Code's
    stdout screen-clear doesn't wipe it.
    """
    g = "\033[38;5;42m"   # lime-green
    d = "\033[38;5;28m"   # darker green (border)
    b = "\033[1m"
    r = "\033[0m"
    lines = [
        "",
        f"{d}  ╭────────────────────────────────────────────────╮{r}",
        f"{d}  │{r}  {b}{g}⚡ ECONOMY LANE{r}  ·  running on cheap off-peak     {d}│{r}",
        f"{d}  │{r}     compute. This run is {b}{g}discounted{r}.            {d}│{r}",
        f"{d}  │{r}     {g}Savings show up in your session view.{r}      {d}│{r}",
        f"{d}  ╰────────────────────────────────────────────────╯{r}",
        "",
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def _require_claude_binary() -> str:
    """Find the `claude` binary on PATH or print a friendly error."""
    path = shutil.which("claude")
    if not path:
        sys.stderr.write(
            "\n  ERROR: `claude` not found on PATH.\n"
            "  Install Claude Code first — see https://claude.com/code\n\n"
        )
        sys.exit(127)
    return path


def _resolve_flags(permission_mode: str | None, extra_args: Iterable[str]) -> list[str]:
    """Decide the permission flags handed to `claude`.

    --dangerously-skip-permissions HARD-WINS over --permission-mode (verified: when both
    are present, skip-permissions bypasses the allow-list entirely). So whenever the
    caller manages permissions — via the `permission_mode` param OR by passing
    `--permission-mode` themselves on the CLI — we must NOT inject skip-permissions.
    A bare invocation keeps it so unattended agents that pass no allow-list don't stall
    on permission prompts.
    """
    if permission_mode:
        return ["--permission-mode", permission_mode]
    if any(a == "--permission-mode" or a.startswith("--permission-mode=") for a in extra_args):
        return []
    return list(_DEFAULT_FLAGS)


def _auto_compact_window(model_id: str) -> int:
    """Effective context window (tokens) for Claude Code's native auto-compact.

    CC has no built-in window for our custom model ids (e.g. "moonshotai/Kimi-K2.6-TEE")
    so its auto-compact never fires — long ir sessions grow unbounded and hard-overflow
    instead of compacting (verified 2026-06-09). Setting CLAUDE_CODE_AUTO_COMPACT_WINDOW
    makes it fire at ~92% of this. Conservative/safe-leaning (proxy compression buffers a
    slight over-estimate; compacting a little early >> never)."""
    m=(model_id or "").lower()
    if "deepseek" in m: return 120_000
    if "kimi" in m or "glm" in m or "minimax" in m: return 200_000
    return 150_000


def _apply_autocompact_env(env: dict, model_id: str) -> dict:
    """Set CLAUDE_CODE_AUTO_COMPACT_WINDOW for our custom model ids; user value wins."""
    if "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in env:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]=str(_auto_compact_window(model_id))
    return env


def launch_through_inferroute(
    model_id: str,
    creds: Credentials,
    extra_args: Iterable[str] = (),
    permission_mode: str | None = None,
) -> None:
    """Exec `claude` with inferroute env + --model pinned.

    Auth strategy:
      * ANTHROPIC_BASE_URL  → inferroute (so chat traffic routes here)
      * ANTHROPIC_AUTH_TOKEN → inferroute key as Bearer (skips Claude Code's
        "Detected a custom API key" safety prompt — that prompt only fires
        when ANTHROPIC_API_KEY is set to a non-Anthropic value).
      * ANTHROPIC_API_KEY    → LEFT UNTOUCHED. The user's existing Anthropic
        subscription key, if set, stays in place so Claude Code's
        first-party-only features (voice tap, etc.) keep working against
        the actual Anthropic API.

    The proxy at api.inferroute.ai accepts both `Authorization: Bearer …`
    and `x-api-key: …`, so this hybrid setup is safe — the routed chat
    path uses Bearer; anything Claude Code routes outside our base_url
    (or anything that uses its own first-party-only auth path) can still
    use the user's real Anthropic key.
    """
    if not creds.is_valid:
        sys.stderr.write(
            "\n  ERROR: no inferroute API key found.\n"
            "  Run `ir login` first.\n\n"
        )
        sys.exit(2)

    # Nested-session guard: if we're already inside a Claude Code session (an agent
    # running `ir` via its Bash tool), launching `claude` would spawn a confusing
    # nested sub-session. Refuse with guidance instead. Override with IR_ALLOW_NESTED=1
    # for deliberate nested use. Read-only commands (ir gate, ir help) never reach here.
    if os.environ.get("CLAUDECODE") == "1" and os.environ.get("IR_ALLOW_NESTED") != "1":
        sys.stderr.write(
            "\n  ir: refusing to launch a nested Claude Code session (CLAUDECODE=1).\n"
            "  You're already inside Claude Code. For research use `ir gate`, "
            "`ir gate --print-env`, or `ir help`.\n"
            "  To force a nested launch, set IR_ALLOW_NESTED=1.\n\n"
        )
        sys.exit(2)

    binary = _require_claude_binary()
    env = os.environ.copy()
    # Economy lane: when IR_LANE=economy (e.g. a deferred-loop gate, `ir gate` cycles),
    # route to the /economy base path so the proxy bills this run at the discount.
    # Otherwise the normal interactive base URL.
    economy = env.get("IR_LANE", "").strip().lower() == "economy"
    # Base URL selection:
    #   • Economy lane → cloud /economy path so the proxy bills at the discount.
    #     Bypasses the local daemon (a long-lived service can't see this launch's
    #     IR_LANE), so economy turns aren't locally recorded.
    #   • Recorder daemon running → route through it (records locally, then
    #     forwards to the cloud).
    #   • Otherwise → talk to the cloud directly (no recording).
    # The cloud applies its own backend fallbacks upstream in every case.
    if economy:
        base_url = creds.api_url.rstrip("/") + "/economy"
    else:
        base_url = _recording_daemon_url() or creds.api_url
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = creds.api_key
    # NOTE: deliberately do NOT pop ANTHROPIC_API_KEY — see docstring.

    # Pin the small/fast slot too. `--model` only pins Claude Code's MAIN
    # model; CC keeps a separate "haiku" slot for background chores (session
    # title generation, is-this-a-new-topic checks, quota/spinner summaries).
    # That slot defaults to a `claude-haiku-*` id the proxy doesn't recognise,
    # so without this the user who typed `ir --model minimax` sees surprise
    # background traffic on a different model in the session viewer. Mirroring
    # the pinned id into the haiku slot keeps those calls on the chosen model.
    # Every session now pins a concrete model, so this always applies.
    #
    # ANTHROPIC_DEFAULT_HAIKU_MODEL is the current (CC v2.x) knob;
    # ANTHROPIC_SMALL_FAST_MODEL is the legacy name older builds still read.
    # Set both so the pin holds regardless of the user's CC version.
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model_id
    env["ANTHROPIC_SMALL_FAST_MODEL"] = model_id

    # Mint a stable per-launch session id and thread it into EVERY Claude Code
    # request via a custom header, so the proxy can tag each usage row and the
    # dashboard link shows exactly this invocation's traffic (even with several
    # `ir` sessions running at once). CC sends ANTHROPIC_CUSTOM_HEADERS on every
    # request — including background "haiku" chores — merged with auth headers.
    # MERGE with any value the user already set (newline-separated) rather than
    # clobbering it.
    session_id = uuid.uuid4().hex
    _headers = []
    _existing = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
    if _existing:
        _headers.append(_existing)
    _headers.append(f"x-inferroute-session: {session_id}")
    # Tag the economy lane via a HEADER too, not only the /economy base URL. The base URL
    # alone is unreliable: CC's SDK drops the base-path segment on most requests (→ /v1/messages,
    # which the gate counts as INTERACTIVE), so only a couple of turns per run were actually
    # billed at the discount. CC forwards ANTHROPIC_CUSTOM_HEADERS on EVERY request (incl. the
    # background haiku slot), and the gate tags `economy = economy_path OR ir-lane: economy`
    # (cc-proxy-prod app.py), so this header makes the discount apply to every turn.
    if economy:
        _headers.append("ir-lane: economy")
        # Present a single-use admission grant so the proxy ACCEPTS this session into economy — the
        # discount is grant-gated, not just tag-gated. Use a pre-issued IR_GRANT (e.g. a deferred
        # loop's `ir gate --print-grant`) if set, else grab one now by polling the gate right before
        # exec (freshest grant → best margin against CC cold start). No grant (red / at cap /
        # unreachable) → the run simply bills at the standard rate.
        _grant = env.get("IR_GRANT", "").strip()
        if not _grant:
            try:
                from .gate import grab_grant
                _grant = grab_grant(creds) or ""
            except Exception:
                _grant = ""
        if _grant:
            _headers.append(f"ir-grant: {_grant}")
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(_headers)

    # CC native auto-compact for custom model ids (see _auto_compact_window);
    # without this, long ir sessions never compact and hard-overflow the context.
    _apply_autocompact_env(env, model_id)

    if economy:
        _print_economy_banner()

    # Print the dashboard link BEFORE handing the terminal to claude.
    _print_session_link(creds.api_url, session_id)

    # Remember this model so a later `ir --resume` can reuse it without the
    # picker (matching `claude --resume`, which never asks for a model).
    _persist_last_model(model_id)

    # A caller-supplied --permission-mode (the `permission_mode` param OR a passthrough
    # --permission-mode on the CLI) means the caller governs permissions, so we must NOT
    # force --dangerously-skip-permissions — it hard-wins over --permission-mode and would
    # bypass the caller's allow-list. Bare invocations keep skip-permissions so unattended
    # agents that pass no allow-list don't stall on prompts. (See _resolve_flags.)
    extra = list(extra_args)
    flags = _resolve_flags(permission_mode, extra)
    # Pin the product strip (⚡ model · lane │ link │ $cost) to CC's status line
    # so it stays visible for the whole session — the pre-launch banner above is
    # hidden by CC's fullscreen TUI (the DEFAULT in 2.1.170) and scrolls away in
    # the inline renderer. No-ops if the user has their own
    # statusLine / --settings, or set IR_NO_STATUSLINE.
    prefix = _gate_strip_prefix(creds.api_url, session_id, model_id, economy)
    # The on-device recorder daemon (when running) writes this session's real
    # cumulative cost here; the status line reads it. No-ops gracefully if the
    # daemon isn't running (file never appears) — see _strip_command.
    cost_file = _record_sessions_dir() / f"{session_id}.cost"
    status_args = _product_strip_settings_args(prefix, extra, cost_file)
    argv = [
        binary,
        *flags,
        "--model", model_id,
        *status_args,
        *extra,
    ]
    # execvpe replaces the current process — claude inherits our terminal.
    os.execvpe(binary, argv, env)


def launch_native_anthropic(extra_args: Iterable[str] = ()) -> None:
    """Exec plain `claude` — DO NOT touch ANTHROPIC_BASE_URL / API_KEY.

    Whatever the user has set globally is what runs. If they're not
    authenticated, claude itself will tell them.

    Still gets the product strip (sans dashboard link — native doesn't route
    through inferroute, so there's no session to link). The relaunch hint here is
    `ir anthropic [--model X]`, which is the piece a user otherwise loses under
    the fullscreen TUI. Same backoff as the gate path.
    """
    binary = _require_claude_binary()
    extra = list(extra_args)
    status_args = _product_strip_settings_args(_native_strip_prefix(extra), extra)
    argv = [binary, *_DEFAULT_FLAGS, *status_args, *extra]
    os.execvp(binary, argv)
