"""`ir login` — paste an inferroute API key, save to disk."""

from __future__ import annotations

import sys
from urllib.parse import urlparse

import httpx

from . import config


SIGNUP_URL = "https://inferroute.ai/api-keys"

# inferroute keys (and legacy cdt_ keys) — used for a cheap local sanity check
# before we ever hit the network, so an accidental paste of random text is
# rejected as "invalid" instead of fired at the server.
KEY_PREFIXES = ("inf_", "cdt_")
MAX_ATTEMPTS = 3


def _mask(key: str) -> str:
    """Show enough of the key to confirm a paste registered, without dumping
    the whole secret to the terminal."""
    if len(key) <= 10:
        return (key[:2] + "…") if key else "…"
    return f"{key[:6]}…{key[-4:]}"


def _looks_like_key(key: str) -> bool:
    return key.startswith(KEY_PREFIXES) and len(key) >= 12 and key.isprintable()


def _prompt_key(prompt: str) -> str | None:
    """Read one API key from the terminal.

    Two problems this guards against:

    1. A pasted blob that contains newlines. ``input()`` consumes only the
       first line; the rest stays in the terminal's input queue and, once the
       program exits, is handed straight to the shell and *executed*. We read
       one line in canonical mode and then ``tcflush`` the input queue so any
       trailing pasted lines are discarded, never reaching bash.

    2. The raw key being echoed across the screen. We turn echo off while
       reading and print a masked confirmation afterwards, so the secret never
       lands in the scrollback.

    Returns the first whitespace-delimited token of the line (the key),
    ``""`` for an empty line, or ``None`` on EOF / Ctrl-C.
    """
    stdin = sys.stdin
    if not stdin.isatty():
        # Piped / non-interactive: just read a line, no terminal tricks.
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        return line.split()[0] if line else ""

    import termios

    fd = stdin.fileno()
    sys.stdout.write(prompt)
    sys.stdout.flush()

    old = termios.tcgetattr(fd)
    try:
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~termios.ECHO  # lflags: disable echo, keep canonical
        termios.tcsetattr(fd, termios.TCSANOW, new)
        line = stdin.readline()
    except (KeyboardInterrupt, EOFError):
        line = ""
    finally:
        # Drop anything still queued — the tail of a multi-line paste — before
        # restoring the terminal, so it can never spill into the shell.
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()

    if not line:
        return None
    line = line.strip()
    return line.split()[0] if line else ""


def _verify(api_url: str, key: str) -> tuple[str, int | None]:
    """Probe /v1/models. Returns (status, n_models):
    status ∈ {"ok", "reject", "unreachable"}."""
    try:
        r = httpx.get(
            f"{api_url}/v1/models",
            headers={"x-api-key": key},
            timeout=10.0,
        )
        if r.status_code in (401, 403):
            return "reject", None
        r.raise_for_status()
        return "ok", len((r.json() or {}).get("data") or [])
    except httpx.HTTPError:
        return "unreachable", None


def run(args=None) -> int:
    print()
    print(f"  Sign up + get a key at {SIGNUP_URL}")
    print(f"  Pasting the key here saves it to {config.CREDS_FILE} (mode 600).")
    print()

    api_url = config.DEFAULT_API_URL
    if hasattr(args, "url") and args.url:
        api_url = args.url.rstrip("/")
        if not urlparse(api_url).scheme.startswith("http"):
            sys.stderr.write(f"  ERROR: bad URL: {api_url}\n")
            return 2

    for attempt in range(1, MAX_ATTEMPTS + 1):
        key = _prompt_key("  inferroute API key: ")
        if key is None:  # Ctrl-C / Ctrl-D / EOF
            return 130
        if not key:
            sys.stderr.write("  No key entered. Try again.\n")
            continue
        if not _looks_like_key(key):
            # Caught a fat-fingered paste before it ever touches the network.
            sys.stderr.write(
                "  ✗ invalid API key — expected something starting with "
                f"`inf_`. Try again.\n"
            )
            continue

        print(f"  Verifying key ({_mask(key)})…")
        status, n_models = _verify(api_url, key)

        if status == "reject":
            remaining = MAX_ATTEMPTS - attempt
            tail = f" ({remaining} attempt{'s' if remaining != 1 else ''} left)" if remaining else ""
            sys.stderr.write(f"  ✗ invalid API key — server rejected it.{tail}\n")
            if remaining:
                continue
            sys.stderr.write("  Re-run `ir login` once you have a valid key.\n")
            return 1

        # "ok" or "unreachable" → save. For unreachable we can't confirm the
        # key, but the user clearly has one in hand; persist and let them retry.
        path = config.save(api_key=key, api_url=api_url)
        print(f"  ✓ saved to {path}")
        if status == "ok":
            print(f"  ✓ {n_models} models available")
        else:
            sys.stderr.write(
                f"  ⚠ couldn't reach {api_url} to verify — saved anyway.\n"
            )
        print()
        print("  Try it:")
        print("    ir                   # open the model picker, then launch")
        print("    ir --model minimax   # pin to MiniMax")
        print("    ir status            # see your usage")
        return 0

    return 1
