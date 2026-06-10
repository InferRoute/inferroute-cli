"""`ir gate` — the deferred-loop gate, as a pace-gate-style exit-code command.

A loop calls this between cycles to ask inferroute "is now a cheap (economy) window?"
It owns ALL the gate protocol in one place — fail-open policy, jitter, and (future)
probabilistic admission / deadline-ramp — so integrated loops upgrade via `pipx upgrade`
instead of every repo carrying stale curl logic.

Exit codes (mirror ~/bin/pace-gate so it drops into existing loops):
  0  → GREEN: run a cycle now (a small jitter is applied to de-sync concurrent loops)
  1  → RED: skip this cycle (not a cheap window)
On error/unreachable: FAIL-OPEN (exit 0) by default so the loop never stalls on us —
  override with --fail-closed for loops that must NOT run off-economy.

Usage in a loop:
    if ir gate; then run_one_cycle; else sleep 30; fi

Compose with the user's own pace-gate:
    ~/bin/pace-gate && ir gate && run_one_cycle    # Max-pace ahead AND inferroute trough

The cycle that runs after a green still needs to EXECUTE on the economy lane to get the
discount — point its Anthropic base URL at `<api_url>/economy` (see `ir gate --print-env`).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from . import config


def _poll(api_url: str, api_key: str, timeout: float) -> dict | None:
    """Call /v1/gate. Returns the parsed JSON dict, or None on any failure."""
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/gate",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def grab_grant(creds, timeout: float = 10.0) -> str | None:
    """Poll the gate; return a single-use economy admission grant if GREEN and under the per-identity
    cap, else None (red / at cap / unreachable → caller runs at the standard rate). No jitter — this
    is a one-shot grab for a launch, not a loop pacer."""
    quote = _poll(creds.api_url, creds.api_key, timeout=timeout)
    if quote and quote.get("go") and quote.get("grant"):
        return str(quote["grant"])
    return None


def cmd_gate(args: list[str]) -> int:
    """Entry point for `ir gate`."""
    fail_closed = "--fail-closed" in args
    quiet = "--quiet" in args or "-q" in args
    # --print-env: emit the economy execution env for the gated cycle, then exit 0.
    #   eval "$(ir gate --print-env)"           → export lines for the current shell
    #   env $(ir gate --print-env --inline) …   → one-line KEY=val prefix for an inline
    #                                             `env … claude` command (mirrors a loop's
    #                                             existing `NATIVE_ENV="env -u …"` idiom)
    if "--print-env" in args:
        creds = config.load()
        if not creds.is_valid:
            sys.stderr.write("ir gate: no API key (run `ir login`)\n")
            return 2
        base = f"{creds.api_url.rstrip('/')}/economy"
        if "--inline" in args:
            print(f"ANTHROPIC_BASE_URL={base} ANTHROPIC_AUTH_TOKEN={creds.api_key}")
        else:
            print(f'export ANTHROPIC_BASE_URL="{base}"')
            print(f'export ANTHROPIC_AUTH_TOKEN="{creds.api_key}"')
        return 0

    # --print-grant: poll, and on GREEN print a single-use admission grant to stdout (exit 0),
    # else print nothing (exit 1). For deferred loops that want to grab the grant once and pass it
    # to the launch via IR_GRANT:  GRANT="$(ir gate --print-grant)" && IR_GRANT="$GRANT" ir --model …
    if "--print-grant" in args:
        creds = config.load()
        if not creds.is_valid:
            return 1
        g = grab_grant(creds)
        if g:
            print(g)
            return 0
        return 1

    creds = config.load()
    if not creds.is_valid:
        sys.stderr.write("ir gate: no API key (run `ir login`)\n")
        # No creds = can't gate; treat like unreachable → honor fail policy.
        return 1 if fail_closed else 0

    quote = _poll(creds.api_url, creds.api_key, timeout=10.0)
    if quote is None:
        if not quiet:
            sys.stderr.write(
                "ir gate: inferroute unreachable → "
                + ("skip (fail-closed)\n" if fail_closed else "run anyway (fail-open)\n")
            )
        return 1 if fail_closed else 0

    if quote.get("go") is True:
        # Jitter de-syncs concurrent loops so they don't all fire on the same green.
        time.sleep(_jitter())
        if not quiet:
            disc = quote.get("discount")
            sys.stderr.write(f"ir gate: GREEN (economy{f', -{int(disc*100)}%' if disc else ''})\n")
        return 0

    if not quiet:
        sys.stderr.write("ir gate: red (peak window) — skip\n")
    return 1


def _jitter() -> float:
    """0–5s jitter. Avoids Math.random ban concerns by using monotonic fractional time."""
    return (time.monotonic() * 1000) % 5
