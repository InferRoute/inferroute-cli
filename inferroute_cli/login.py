"""`ir login` — paste an inferroute API key, save to disk."""

from __future__ import annotations

import sys
from urllib.parse import urlparse

import httpx

from . import config


SIGNUP_URL = "https://inferroute.ai"


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

    # Visible prompt (not getpass): the key is pasted, and hiding it just makes
    # users think the paste didn't register. The inf_ key is rotatable from the
    # dashboard, so echoing it to the terminal is an acceptable trade for clarity.
    try:
        key = input("  inferroute API key: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return 130

    if not key:
        sys.stderr.write("  No key entered. Aborted.\n")
        return 2

    # Optional sanity probe — call /v1/models with the key.
    print("  Verifying key…")
    try:
        r = httpx.get(
            f"{api_url}/v1/models",
            headers={"x-api-key": key},
            timeout=10.0,
        )
        if r.status_code == 401:
            sys.stderr.write("  ✗ key rejected by server (401). Try again.\n")
            return 1
        r.raise_for_status()
        n_models = len((r.json() or {}).get("data") or [])
    except httpx.HTTPError as e:
        sys.stderr.write(f"  ⚠ couldn't reach {api_url}: {e}\n")
        sys.stderr.write("    Saving the key anyway. You can try again later.\n")
        n_models = None

    path = config.save(api_key=key, api_url=api_url)
    print(f"  ✓ saved to {path}")
    if n_models is not None:
        print(f"  ✓ {n_models} models available")
    print()
    print("  Try it:")
    print("    ir                   # open the model picker, then launch")
    print("    ir --model minimax   # pin to MiniMax")
    print("    ir status            # see your usage")
    return 0
