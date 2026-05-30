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
from typing import Iterable

from .config import Credentials


_DEFAULT_FLAGS = ["--dangerously-skip-permissions"]


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
    """Exec `claude` with inferroute env + --model pinned."""
    if not creds.is_valid:
        sys.stderr.write(
            "\n  ERROR: no inferroute API key found.\n"
            "  Run `ir login` first.\n\n"
        )
        sys.exit(2)

    binary = _require_claude_binary()
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = creds.api_url
    env["ANTHROPIC_API_KEY"] = creds.api_key

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
