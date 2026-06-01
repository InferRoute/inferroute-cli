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
import time
from pathlib import Path
from typing import Iterable

from .config import Credentials


_DEFAULT_FLAGS = ["--dangerously-skip-permissions"]


def _print_session_link(api_url: str) -> None:
    """Print a clickable URL to view this session's traffic on the dashboard.

    The session page at <site>/dashboard/session/[id] uses a `from-<unix_ms>`
    slug — it aggregates every usage_records row since that timestamp. So
    capturing "now" here gives the user a stable link they can revisit later
    to see exactly the requests this `ir` invocation produced.

    We also persist the URL to ~/.config/inferroute/last_session for retrieval
    via `ir status` or shell history.
    """
    now_ms = int(time.time() * 1000)
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
    url = f"{site.rstrip('/')}/session/from-{now_ms}"

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

    binary = _require_claude_binary()
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = creds.api_url
    env["ANTHROPIC_AUTH_TOKEN"] = creds.api_key
    # NOTE: deliberately do NOT pop ANTHROPIC_API_KEY — see docstring.

    # Economy lane banner (cosmetic) — show it before the dashboard link when the
    # caller tagged this run economy (IR_LANE=economy, e.g. a deferred-loop gate).
    if env.get("IR_LANE", "").strip().lower() == "economy":
        _print_economy_banner()

    # Print the dashboard link BEFORE handing the terminal to claude.
    _print_session_link(creds.api_url)

    argv = [
        binary,
        *_DEFAULT_FLAGS,
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
