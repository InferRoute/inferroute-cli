"""`ir add recording` — install the optional on-device recorder.

Recording is fully local and fully private. When enabled, a small daemon runs
on localhost:5005; `ir` routes your sessions through it so it can log, on YOUR
machine only, which model you picked for each task and how the turn went. That
local corpus is yours — to inspect (`ir data show`), export, or wipe
(`ir data wipe`). The corpus is never uploaded; inferroute keeps only a one-way
hash of each turn (a fingerprint, never the text).

What this does, in order:
  1. Ask how much to record. Default: full — keeps the prompt text locally, which
     is what lets it learn your preferences; it never leaves the machine.
     'cost-only' (level=off) records NOTHING but still runs the daemon so the
     status line can show your real per-session cost.
  2. Install the `[local]` deps (fastapi, uvicorn) if missing.
  3. Install a systemd user unit (Linux) / launchd plist (macOS) that runs the
     recorder daemon, with the chosen record level baked in, and start it.
  4. Append `ANTHROPIC_BASE_URL=http://localhost:5005` to the user's shell rc so
     plain `claude` also flows through the recorder. `--no-shell-edit` prints the
     line instead.

There is NO classifier and NO routing — the daemon is a pure pass-through
recorder. See shared-docs/inferroute/local-decision-recorder-spec.md.

Every step is idempotent. `local-routing` is accepted as a deprecated alias for
`recording`.
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import textwrap
from pathlib import Path

LOCAL_BASE_URL = "http://localhost:5005"

# The systemd user unit / launchd label for the recorder daemon. We use the
# established name `inferroute-local.service` so this installer and any
# pre-existing (hand-crafted) unit converge on one name — no second daemon, no
# :5005 collision. The record LEVEL is applied via a drop-in (see below) rather
# than baked into the base unit, so re-running `ir add recording` never clobbers
# a richer hand-written unit.
SERVICE_NAME = "inferroute-local.service"
# Marker written into base units WE create, so `ir remove recording` only ever
# deletes installer-created units and leaves hand-crafted ones in place.
UNIT_MARKER = "# Managed-by: ir add recording"
# Drop-in that carries just the record level. Layers on top of the base unit.
DROPIN_NAME = "10-record-level.conf"

# Shell rc files we know how to edit. Order = preference.
SHELL_RC_FILES = {
    "zsh":  Path.home() / ".zshrc",
    "bash": Path.home() / ".bashrc",
    "fish": Path.home() / ".config/fish/config.fish",
}

# Markers kept stable so `ir remove` can clean up. The legacy pair is recognised
# too, so blocks written by older versions are still removable.
SHELL_EDIT_MARKER_BEGIN = "# >>> inferroute recording >>>"
SHELL_EDIT_MARKER_END   = "# <<< inferroute recording <<<"
_LEGACY_MARKER_BEGIN = "# >>> inferroute local-routing >>>"
_LEGACY_MARKER_END   = "# <<< inferroute local-routing <<<"

_VALID_LEVELS = ("metadata", "full", "off")


def cmd_add(rest: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ir add", description="Add an optional feature.")
    ap.add_argument(
        "feature", choices=["recording", "local-routing"],
        help="Which feature to add (use 'recording').",
    )
    ap.add_argument(
        "--level", choices=_VALID_LEVELS, default=None,
        help="Recording level (skips the prompt). full (default) | metadata | off.",
    )
    ap.add_argument(
        "--no-shell-edit", action="store_true",
        help="Don't modify your shell rc; print the env-var line instead.",
    )
    ap.add_argument(
        "--no-service", action="store_true",
        help="Skip installing the systemd/launchd unit (daemon won't auto-start).",
    )
    ap.add_argument(
        "--yes", "-y", action="store_true",
        help="Accept defaults without prompting (level=full unless --level given).",
    )
    ns = ap.parse_args(rest)

    if ns.feature == "local-routing":
        print("  note: `local-routing` is now `recording` (no router anymore). "
              "Installing recording.")
    return _add_recording(ns)


# ──────────────────────────────────────────────────────────────────────────
# recording installer
# ──────────────────────────────────────────────────────────────────────────

def _add_recording(ns) -> int:
    print()
    print("  Add local recording")
    print("  ───────────────────")

    level = ns.level or _prompt_level(ns.yes)
    if level == "abort":
        print("\n  Nothing was changed. `ir` keeps working as the lightweight launcher.")
        print("  (Already have the daemon? `ir remove recording` takes it off.)")
        return 0
    # level == "off" is NOT "install nothing" — it's COST-ONLY: the daemon still
    # runs (so the status line can show the real session cost) but records no
    # corpus. The only way to have no daemon at all is to never add it / remove it.
    if level == "off":
        print("\n  Cost-only mode: the daemon will run to show this machine's real")
        print("  session cost in Claude Code's status line, and record NOTHING else")
        print("  (no prompts, no responses, no events). Turn off entirely with")
        print("  `ir remove recording`.")

    # Step 1: ensure the [local] deps are installed.
    if not _local_extra_installed():
        if not _confirm_pip_install(ns.yes):
            print("  Aborted — no pip install run.")
            return 1
        rc = _pip_install_local_extra()
        if rc != 0:
            print(f"\n  pip install failed (exit {rc}). Fix the error above and re-run.")
            return rc
    else:
        print("  [1/3] Python deps already installed.")

    # Step 2: install + start the service with the chosen record level.
    if ns.no_service:
        print("  [2/3] Skipping service install (--no-service).")
        print(f"        Run: INFERROUTE_RECORD_LEVEL={level} inferroute-daemon")
    else:
        rc = _install_user_service(level)
        if rc != 0:
            print("        ✗ Service install failed. You can run the daemon manually:")
            print(f"          INFERROUTE_RECORD_LEVEL={level} inferroute-daemon")

    # Step 3: shell rc edit.
    if ns.no_shell_edit:
        _print_env_var_block()
    else:
        rc = _edit_shell_rc()
        if rc != 0:
            _print_env_var_block()

    _print_done_banner(level)
    return 0


def _prompt_level(skip_prompt: bool) -> str:
    if skip_prompt:
        return "full"
    print(textwrap.dedent("""
      Inferroute can learn YOUR model preferences over time, to route for you
      later. To do that it records, on THIS machine only:
        • which model you pick for each task
        • the prompt + how the turn went

      ✔ The corpus stays in ~/.inferroute on your computer — never uploaded.
      ✔ inferroute keeps only a one-way hash of each turn — a fingerprint, never
        the text. The prompts & responses themselves never leave this machine.
      ✔ Inspect any time:  ir data show
      ✔ Delete any time:   ir data wipe

      'full' keeps the prompt text, which is what actually lets it learn your
      preferences later — and it never leaves this machine. 'minimal' keeps only
      the model choice + outcome (no prompt text), which is lighter but can't
      train a personal router. 'cost-only' records NOTHING — the daemon just runs
      so the status line can show your real session cost.

      Record locally to build your own router?
        [1] Yes — full: choices, outcomes + prompt text   (recommended)
        [2] Yes — minimal: choices + outcomes only, no prompt text
        [3] No  — cost-only: show real cost, store nothing
        [4] Don't install the daemon at all
    """))
    try:
        ans = input("        Choose [1]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return "abort"
    return {"": "full", "1": "full", "2": "metadata", "3": "off", "4": "abort"}.get(ans, "full")


# ----- Step 1 helpers --------------------------------------------------------

def _local_extra_installed() -> bool:
    """True iff the recorder daemon's runtime deps are importable in this env."""
    try:
        import fastapi   # noqa: F401
        import uvicorn   # noqa: F401
        import httpx     # noqa: F401
        return True
    except ImportError:
        return False


def _confirm_pip_install(skip_prompt: bool) -> bool:
    print(textwrap.dedent("""\
      [1/3] This installs the recorder daemon's deps in the current env:
              fastapi, uvicorn, httpx   (~15 MB)
    """))
    if skip_prompt:
        return True
    try:
        ans = input("        Install now? [Y/n] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return ans in {"", "y", "yes"}


def _pip_install_local_extra() -> int:
    cmd = [sys.executable, "-m", "pip", "install", "inferroute[local]"]
    print(f"        Running: {' '.join(cmd)}")
    return subprocess.call(cmd)


# ----- Step 2 helpers (service install) --------------------------------------

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=inferroute-local daemon (Claude Code traffic recorder on :5005)
{marker}
After=network.target

[Service]
Type=simple
ExecStart={daemon_path} --port 5005
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""

DROPIN_TEMPLATE = """\
# Written by `ir add recording`. Sets the record level without touching the
# base unit, so a hand-crafted base unit is preserved. Remove via `ir remove
# recording`.
[Service]
Environment=INFERROUTE_RECORD_LEVEL={level}
"""


def _install_user_service(level: str) -> int:
    sysname = platform.system()
    if sysname == "Linux":
        return _install_systemd_unit(level)
    if sysname == "Darwin":
        return _install_launchd_plist(level)
    print(f"        Unsupported platform for auto-start: {sysname}")
    print(f"        Run: INFERROUTE_RECORD_LEVEL={level} inferroute-daemon")
    return 1


def _install_systemd_unit(level: str) -> int:
    daemon_path = _which("inferroute-daemon")
    if daemon_path is None:
        print("        ✗ `inferroute-daemon` not on PATH. Pip install may not have linked the script.")
        return 1
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / SERVICE_NAME
    unit_dir.mkdir(parents=True, exist_ok=True)

    # Only write the base unit if there isn't one already — never overwrite a
    # hand-crafted unit (e.g. one with a richer EnvironmentFile). The record
    # level always goes in a drop-in, which layers on top of whatever base exists.
    created_base = False
    if not unit_path.exists():
        unit_path.write_text(
            SYSTEMD_UNIT_TEMPLATE.format(daemon_path=daemon_path, marker=UNIT_MARKER)
        )
        created_base = True
    else:
        print(f"  [2/3] Using existing unit {unit_path} (preserved).")

    dropin_dir = unit_dir / f"{SERVICE_NAME}.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / DROPIN_NAME).write_text(DROPIN_TEMPLATE.format(level=level))

    cmds = [["systemctl", "--user", "daemon-reload"]]
    if created_base:
        cmds.append(["systemctl", "--user", "enable", SERVICE_NAME])
    cmds.append(["systemctl", "--user", "restart", SERVICE_NAME])
    for cmd in cmds:
        rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc != 0:
            print(f"        ✗ `{' '.join(cmd)}` failed (exit {rc}).")
            return rc
    where = "installed" if created_base else "updated"
    print(f"  [2/3] Recorder {where} (level={level} via drop-in)")
    print(f"        Running on {LOCAL_BASE_URL} (systemctl --user status {SERVICE_NAME})")
    return 0


def _install_launchd_plist(level: str) -> int:
    daemon_path = _which("inferroute-daemon")
    if daemon_path is None:
        print("        ✗ `inferroute-daemon` not on PATH.")
        return 1
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "ai.inferroute.daemon.plist"
    plist_path.write_text(textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key><string>ai.inferroute.daemon</string>
          <key>ProgramArguments</key>
          <array>
            <string>{daemon_path}</string>
            <string>--port</string><string>5005</string>
          </array>
          <key>EnvironmentVariables</key>
          <dict><key>INFERROUTE_RECORD_LEVEL</key><string>{level}</string></dict>
          <key>RunAtLoad</key><true/>
          <key>KeepAlive</key><true/>
        </dict>
        </plist>
    """))
    subprocess.call(["launchctl", "unload", str(plist_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = subprocess.call(["launchctl", "load", str(plist_path)])
    if rc != 0:
        print(f"        ✗ launchctl load failed (exit {rc}).")
        return rc
    print(f"  [2/3] Recorder installed at {plist_path} (level={level})")
    print(f"        Running on {LOCAL_BASE_URL}")
    return 0


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


# ----- Step 3 helpers (shell rc) ---------------------------------------------

def _edit_shell_rc() -> int:
    shell_name = _detect_shell()
    rc_path = SHELL_RC_FILES.get(shell_name)
    if rc_path is None:
        print(f"  [3/3] Shell '{shell_name}' not auto-supported; printing env line:")
        _print_env_var_block()
        return 1

    current = rc_path.read_text() if rc_path.exists() else ""
    if SHELL_EDIT_MARKER_BEGIN in current or _LEGACY_MARKER_BEGIN in current:
        print(f"  [3/3] Shell rc already points at the local daemon — leaving as-is.")
        print(f"        ({rc_path})")
        return 0

    block = (
        f"\n{SHELL_EDIT_MARKER_BEGIN}\n"
        f"export ANTHROPIC_BASE_URL={LOCAL_BASE_URL}\n"
        f"{SHELL_EDIT_MARKER_END}\n"
    )
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with rc_path.open("a", encoding="utf-8") as f:
        f.write(block)
    print(f"  [3/3] Appended ANTHROPIC_BASE_URL to {rc_path}")
    print(f"        Open a new shell or run: source {rc_path}")
    return 0


def _detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    return Path(shell).name or "bash"


def _print_env_var_block() -> None:
    print()
    print("        Add this to your shell rc to point Claude Code at the local recorder:")
    print()
    print(f"            export ANTHROPIC_BASE_URL={LOCAL_BASE_URL}")
    print()


def _print_done_banner(level: str) -> None:
    print()
    if level == "off":
        print("  Done. Cost-only daemon is ON (recording corpus: OFF).")
        print("  Your real session cost now shows in Claude Code's status line.")
        print("  Nothing else is stored. Manage:")
        print("    ir add recording --level full   # also record, to train a router")
        print("    ir remove recording             # stop the daemon entirely")
    else:
        print(f"  Done. Local recording is ON (level: {level}).")
        print("  The corpus stays in ~/.inferroute on this machine; inferroute keeps")
        print("  only a one-way hash of each turn (a fingerprint, never the text).")
        print("  Verify / manage:")
        print("    ir data show     # what's been recorded (counts, models, size)")
        print("    ir data wipe     # delete it all")
        print("    ir remove recording")
    print()
