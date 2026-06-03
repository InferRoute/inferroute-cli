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


def _print_session_link(api_url: str, session_id: str) -> None:
    """Print a clickable URL to view this session's traffic on the dashboard.

    The session page at <site>/session/[id] is keyed by the per-launch
    `session_id` we mint below and inject into every Claude Code request (via
    ANTHROPIC_CUSTOM_HEADERS). The proxy tags each usage_records row with it, so
    the link shows exactly — and only — the requests this `ir` invocation
    produced, even when several `ir` sessions run concurrently. (Older links use
    a `from-<unix_ms>` timestamp-window slug; the dashboard still accepts those.)

    We also persist the URL to ~/.config/inferroute/last_session for retrieval
    via `ir status` or shell history.
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
    url = f"{site.rstrip('/')}/session/{session_id}"

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
    _session_header = f"x-inferroute-session: {session_id}"
    _existing_headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
    env["ANTHROPIC_CUSTOM_HEADERS"] = (
        f"{_existing_headers}\n{_session_header}" if _existing_headers else _session_header
    )

    if economy:
        _print_economy_banner()

    # Print the dashboard link BEFORE handing the terminal to claude.
    _print_session_link(creds.api_url, session_id)

    # permission_mode (e.g. "plan") makes Claude Code propose before acting — the
    # plan-approval IS the gate, so we drop --dangerously-skip-permissions (it would
    # bypass the very gate we want). Otherwise keep the default skip-permissions.
    if permission_mode:
        flags = ["--permission-mode", permission_mode]
    else:
        flags = list(_DEFAULT_FLAGS)
    argv = [
        binary,
        *flags,
        "--model", model_id,
        *list(extra_args),
    ]
    # execvpe replaces the current process — claude inherits our terminal.
    os.execvpe(binary, argv, env)


def launch_native_anthropic(extra_args: Iterable[str] = ()) -> None:
    """Exec plain `claude` — DO NOT touch ANTHROPIC_BASE_URL / API_KEY.

    Whatever the user has set globally is what runs. If they're not
    authenticated, claude itself will tell them.
    """
    binary = _require_claude_binary()
    argv = [binary, *_DEFAULT_FLAGS, *list(extra_args)]
    os.execvp(binary, argv)
