"""`ir` — top-level dispatcher.

Style note: every routed command path is dead-simple — resolve creds,
build the argv, exec claude. The "smarts" live server-side in the
inferroute proxy; this CLI is just a launcher.
"""

from __future__ import annotations

import sys

from . import __version__, config, help as help_mod, launch, login as login_mod, models


def _print_unknown(cmd: str) -> int:
    sys.stderr.write(
        f"\n  unknown command: ir {cmd}\n"
        f"  run `ir help` for the full list\n\n"
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # ── Bare `ir` → auto-route (same as `ir auto`) ─────────────────────
    if not args:
        creds = config.load()
        if not creds.is_valid:
            sys.stderr.write(
                "\n  No inferroute key configured.\n"
                "  Run `ir login` to set one up, or `ir help` for options.\n\n"
            )
            return 2
        auto = models.get("auto")
        if auto is None:
            sys.stderr.write("  internal error: 'auto' alias missing\n")
            return 1
        launch.launch_through_inferroute(auto.model_id, creds)
        return 0  # never reached — exec replaces process

    cmd, rest = args[0], args[1:]

    # ── Global flags handled at top-level ──────────────────────────────
    if cmd in ("-h", "--help", "help"):
        return help_mod.run()
    if cmd in ("-v", "--version"):
        print(f"ir {__version__}")
        return 0

    # ── Account / utility commands ─────────────────────────────────────
    if cmd == "login":
        import argparse
        ap = argparse.ArgumentParser(prog="ir login")
        ap.add_argument("--url", default=None, help="override INFERROUTE_API_URL")
        ns = ap.parse_args(rest)
        return login_mod.run(ns)

    if cmd == "status":
        from . import status as status_mod
        return status_mod.run()

    if cmd == "choose":
        from . import choose as choose_mod
        return choose_mod.run()

    if cmd == "anthropic":
        # Escape hatch — no env, no key check, no inferroute touch.
        launch.launch_native_anthropic(extra_args=rest)
        return 0  # never reached

    # ── Routed model shortcuts ─────────────────────────────────────────
    alias = models.get(cmd)
    if alias is not None:
        creds = config.load()
        launch.launch_through_inferroute(alias.model_id, creds, extra_args=rest)
        return 0  # never reached

    return _print_unknown(cmd)


if __name__ == "__main__":
    sys.exit(main())
