"""`ir remove recording` — symmetric reverse of `ir add recording`.

What this does:
  1. Stop + disable the systemd unit (Linux) / launchd plist (macOS).
  2. Remove the rc-edit block from the user's shell config (recognises both the
     current `# >>> inferroute recording >>>` markers and the legacy
     `# >>> inferroute local-routing >>>` ones).
  3. Optionally delete the local recorded data (off by default — `--purge`).
  4. Optionally uninstall the [local] pip deps (off by default — `--uninstall-deps`).

Default behavior is CONSERVATIVE: stop the daemon and disconnect the shell, but
leave the recorded corpus on disk in case the user wants to come back. `--purge`
is the "really, all of it" flag. `local-routing` is accepted as a deprecated
alias for `recording`.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .add import (
    SHELL_EDIT_MARKER_BEGIN,
    SHELL_EDIT_MARKER_END,
    _LEGACY_MARKER_BEGIN,
    _LEGACY_MARKER_END,
    SHELL_RC_FILES,
    _detect_shell,
)


def cmd_remove(rest: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ir remove")
    ap.add_argument("feature", choices=["recording", "local-routing"])
    ap.add_argument("--purge", action="store_true",
                    help="Also delete the local recorded data (~/.inferroute events + blobs).")
    ap.add_argument("--uninstall-deps", action="store_true",
                    help="Also uninstall the [local] pip deps (fastapi, uvicorn, httpx).")
    ns = ap.parse_args(rest)
    return _remove_recording(ns)


def _remove_recording(ns) -> int:
    print()
    print("  Removing recording")
    print("  ──────────────────")

    # Step 1: stop + disable the user service.
    sysname = platform.system()
    if sysname == "Linux":
        _stop_systemd()
    elif sysname == "Darwin":
        _stop_launchd()
    else:
        print(f"  [1/3] Platform {sysname}: no auto-managed service to stop.")

    # Step 2: remove the shell rc block.
    _undo_shell_rc()

    # Step 3: optional purge of recorded data + any legacy artifacts.
    if ns.purge:
        removed_any = False
        for path in [
            Path.home() / ".inferroute" / "events",
            Path.home() / ".inferroute" / "blobs",
            Path.home() / ".inferroute" / "derived",
            Path.home() / ".inferroute" / "logs",            # legacy decision logs
            Path.home() / ".inferroute" / "models",          # legacy classifier
        ]:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                print(f"  [3/3] Removed {path}")
                removed_any = True
        if not removed_any:
            print("  [3/3] Nothing to purge.")
    else:
        print("  [3/3] Leaving recorded data on disk (use --purge to delete).")

    if ns.uninstall_deps:
        # We don't uninstall `inferroute` itself — that'd remove `ir`. Just the
        # optional deps. Pip has no "remove extras" verb, so list them.
        cmd = [sys.executable, "-m", "pip", "uninstall", "-y",
               "fastapi", "uvicorn", "httpx"]
        subprocess.call(cmd)
        print("        Uninstalled [local] pip deps.")

    print()
    print("  Done. Recording removed.")
    print("  `ir` continues to work as the lightweight launcher.")
    print()
    return 0


# ----- service stop --------------------------------------------------------

def _stop_systemd() -> None:
    unit_path = Path.home() / ".config" / "systemd" / "user" / "inferroute.service"
    if not unit_path.exists():
        print("  [1/3] No systemd unit installed — skipping.")
        return
    for cmd in (
        ["systemctl", "--user", "stop", "inferroute.service"],
        ["systemctl", "--user", "disable", "inferroute.service"],
    ):
        subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    unit_path.unlink()
    subprocess.call(["systemctl", "--user", "daemon-reload"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  [1/3] Stopped + removed {unit_path}")


def _stop_launchd() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "ai.inferroute.daemon.plist"
    if not plist_path.exists():
        print("  [1/3] No launchd plist installed — skipping.")
        return
    subprocess.call(["launchctl", "unload", str(plist_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    plist_path.unlink()
    print(f"  [1/3] Stopped + removed {plist_path}")


# ----- shell rc undo --------------------------------------------------------

def _undo_shell_rc() -> None:
    rc_path = SHELL_RC_FILES.get(_detect_shell())
    if rc_path is None or not rc_path.exists():
        print("  [2/3] No managed shell rc detected — skipping.")
        return
    text = rc_path.read_text()
    changed = False
    for begin, end in (
        (SHELL_EDIT_MARKER_BEGIN, SHELL_EDIT_MARKER_END),
        (_LEGACY_MARKER_BEGIN, _LEGACY_MARKER_END),
    ):
        text, did = _excise_block(text, begin, end, rc_path)
        changed = changed or did
    if changed:
        rc_path.write_text(text)
        print(f"        Open a new shell to pick up the change.")
    else:
        print(f"  [2/3] No inferroute block in {rc_path} — skipping.")


def _excise_block(text: str, begin: str, end: str, rc_path: Path) -> tuple[str, bool]:
    start = text.find(begin)
    if start == -1:
        return text, False
    stop = text.find(end, start)
    if stop == -1:
        print(f"  [2/3] Found begin marker but no end marker in {rc_path} — skip (edit manually).")
        return text, False
    stop += len(end)
    while stop < len(text) and text[stop] == "\n":
        stop += 1
    if start > 0 and text[start - 1] == "\n":
        start -= 1
    print(f"  [2/3] Removed inferroute block from {rc_path}")
    return text[:start] + text[stop:], True
