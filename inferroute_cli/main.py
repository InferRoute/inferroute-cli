"""`ir` — top-level dispatcher.

Style note: every routed command path is dead-simple — resolve creds,
build the argv, exec claude. The "smarts" live server-side in the
inferroute proxy; this CLI is just a launcher.
"""

from __future__ import annotations

import sys

from . import __version__, config, help as help_mod, launch, login as login_mod, models  # noqa: F401


def _print_unknown(cmd: str) -> int:
    sys.stderr.write(
        f"\n  unknown command: ir {cmd}\n"
        f"  run `ir help` for the full list\n\n"
    )
    return 2


def _extract_model_override(args: list[str]) -> tuple[str | None, list[str]]:
    """Pull `--model X` / `--model=X` out of an argv slice and translate.

    Returns (resolved_model_id or None, remaining_args). If the user-supplied
    value matches one of the friendly short names in `models.ALIASES`, we
    translate it to the canonical model_id; otherwise the value passes
    through verbatim so users can pin specific models the CLI doesn't know
    about (e.g. `--model claude-opus-4-8`).

    Handles `--model X` and `--model=X`. Does NOT mutate input. Add a `-m`
    short form here if claude grows one.
    """
    out: list[str] = []
    raw: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--model" and i + 1 < len(args):
            raw = args[i + 1]
            i += 2
            continue
        if a.startswith("--model="):
            raw = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    resolved = _resolve_model_name(raw) if raw is not None else None
    return resolved, out


def _resolve_model_name(name: str) -> str:
    """`minimax` → `MiniMax-M2.7`. Unknown names pass through verbatim.

    This is the ONE place where friendly short names get translated. Anywhere
    else that wants the same behavior should call this rather than
    re-implementing it.
    """
    alias = models.get(name)
    return alias.model_id if alias is not None else name


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # ── Bare `ir` → auto-route (same as `ir auto`) ─────────────────────
    # Also covers `ir --model foo`, `ir --effort high`, `ir --model=X --any-claude-flag`
    # — anything where the first arg is a flag rather than a subcommand. In
    # those cases there's no subcommand to dispatch on; we treat the whole
    # argv as passthrough to claude and route via 'auto' unless the user
    # supplied their own --model.
    if not args or args[0].startswith("-"):
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
        user_model, passthrough = _extract_model_override(args)
        model = user_model or auto.model_id
        launch.launch_through_inferroute(model, creds, extra_args=passthrough)
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

    if cmd == "gate":
        from . import gate as gate_mod
        return gate_mod.cmd_gate(rest)

    if cmd in ("integrate-deferral-gate", "integrate"):
        from . import integrate as integrate_mod
        return integrate_mod.cmd_integrate(rest)

    if cmd == "add":
        # `ir add local-routing` — install the optional on-device daemon.
        # Import lazily so the launcher cold-start stays cheap.
        from . import add as add_mod
        return add_mod.cmd_add(rest)

    if cmd == "remove":
        # Symmetric uninstall. `--purge` wipes model + logs too.
        from . import remove as remove_mod
        return remove_mod.cmd_remove(rest)

    if cmd == "anthropic":
        # Escape hatch — no env, no key check, no inferroute touch.
        launch.launch_native_anthropic(extra_args=rest)
        return 0  # never reached

    # Model selection is now `ir --model NAME`, not `ir <alias>`. If the user
    # typed an old-style alias subcommand, point them at the new form.
    alias = models.get(cmd)
    if alias is not None:
        sys.stderr.write(
            f"\n  `ir {cmd}` was removed in v0.3.0 — model selection is now a flag.\n"
            f"  Run: ir --model {cmd}\n"
            f"  (or just `ir` to let inferroute auto-route)\n\n"
        )
        return 2

    return _print_unknown(cmd)


if __name__ == "__main__":
    sys.exit(main())
