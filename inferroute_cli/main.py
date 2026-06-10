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


# Claude Code's resume/continue flags. When one is present and the user pinned
# no model, `ir` resumes straight through inferroute on the last-used model —
# no model picker — so `ir --resume` feels like `claude --resume`.
_RESUME_FLAGS = frozenset({"--resume", "-r", "--continue", "-c"})


def _is_resume(args: list[str]) -> bool:
    """True if argv carries a resume/continue flag (`--resume [id]`, `-r`, `--continue`, `-c`)."""
    return any(a in _RESUME_FLAGS or a.startswith("--resume=") for a in args)


def _resolve_model_name(name: str) -> str:
    """`minimax` → `MiniMax-M2.7`. Unknown names pass through verbatim.

    This is the ONE place where friendly short names get translated. Anywhere
    else that wants the same behavior should call this rather than
    re-implementing it.
    """
    alias = models.get(name)
    return alias.model_id if alias is not None else name


def main(argv: list[str] | None = None) -> int:
    import os
    args = list(sys.argv[1:] if argv is None else argv)

    # `--economy` (clean first-class form) selects the economy lane — equivalent to the
    # `IR_LANE=economy ir …` env prefix but far less error-prone. Consume it here (so it's
    # not passed through to claude) and set IR_LANE, which launch.py already honors.
    if "--economy" in args or "--econ" in args:
        args = [a for a in args if a not in ("--economy", "--econ")]
        os.environ["IR_LANE"] = "economy"

    # ── Global help / version — honored as the FIRST token, dashed or not.
    # Must run before the bare-flag picker branch below, which would otherwise
    # treat a leading `--help`/`--version` as "flags, no model" and open the
    # picker. `ir --help`, `ir -h`, `ir help`, `ir --version`, `ir -v` all work.
    if args and args[0] in ("-h", "--help", "help"):
        return help_mod.run()
    if args and args[0] in ("-v", "--version"):
        print(f"ir {__version__}")
        return 0

    # ── Bare `ir` (or `ir <flags>` with no model) → interactive picker ──
    # Also covers `ir --effort high`, `ir --model=X --any-claude-flag` — anything
    # where the first arg is a flag rather than a subcommand. There's no
    # subcommand to dispatch on; if the user pinned a model we launch it, else
    # we open the picker. (No auto-route — the user always chooses.)
    if not args or args[0].startswith("-"):
        user_model, passthrough = _extract_model_override(args)
        # Launch directly (no picker) when the user either pinned a model OR is
        # resuming/continuing — `ir --resume` should behave like `claude --resume`,
        # which never asks for a model. On resume we reuse the last-used model so
        # the proxy keeps routing (and the haiku slot / auto-compact window stay
        # pinned); an explicit `--model` always wins. With no model to fall back on
        # (resume before any prior launch) we drop to the picker, which still
        # carries the resume flag through to claude.
        model = user_model
        if model is None and _is_resume(passthrough):
            model = launch.last_model()
        if model is not None:
            # Explicit pin or resume-with-remembered-model → launch directly.
            creds = config.load()
            if not creds.is_valid:
                sys.stderr.write(
                    "\n  No inferroute key configured.\n"
                    "  Run `ir login` to set one up, or `ir help` for options.\n\n"
                )
                return 2
            launch.launch_through_inferroute(model, creds, extra_args=passthrough)
            return 0  # never reached — exec replaces process
        # No model specified → interactive picker so the user chooses one.
        from . import choose as choose_mod
        return choose_mod.run(passthrough)

    cmd, rest = args[0], args[1:]

    # ── Account / utility commands ─────────────────────────────────────
    if cmd == "login":
        import argparse
        ap = argparse.ArgumentParser(prog="ir login")
        ap.add_argument("--url", default=None, help="override INFERROUTE_API_URL")
        ns = ap.parse_args(rest)
        return login_mod.run(ns)

    if cmd == "logout":
        from . import logout as logout_mod
        return logout_mod.run(rest)

    if cmd == "setup":
        # One-shot guided onboarding: login + optional recording.
        from . import setup as setup_mod
        return setup_mod.run(rest)

    if cmd == "status":
        from . import status as status_mod
        return status_mod.run()

    if cmd == "choose":
        from . import choose as choose_mod
        return choose_mod.run(rest)

    if cmd == "gate":
        from . import gate as gate_mod
        return gate_mod.cmd_gate(rest)

    if cmd in ("integrate-deferral-gate", "integrate"):
        from . import integrate as integrate_mod
        return integrate_mod.cmd_integrate(rest)

    if cmd == "add":
        # `ir add recording` — install the optional on-device recorder daemon.
        # Import lazily so the launcher cold-start stays cheap.
        from . import add as add_mod
        return add_mod.cmd_add(rest)

    if cmd == "remove":
        # Symmetric uninstall. `--purge` wipes recorded data too.
        from . import remove as remove_mod
        return remove_mod.cmd_remove(rest)

    if cmd == "data":
        # `ir data show|export|wipe` — manage the local recorded corpus.
        from . import data as data_mod
        return data_mod.cmd_data(rest)

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
            f"  (or just `ir` to open the model picker)\n\n"
        )
        return 2

    return _print_unknown(cmd)


if __name__ == "__main__":
    sys.exit(main())
