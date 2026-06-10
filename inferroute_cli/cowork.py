"""`ir cowork` — InferRoute's everyday-work surface, powered by goose.

goose (https://github.com/block/goose, Apache-2.0) is an open-source agent with
a desktop app *and* a CLI. `ir cowork` wires it to InferRoute and launches it:

  • provider  → anthropic (goose speaks the Anthropic Messages API, like Claude Code)
  • routing   → the on-device recorder daemon when it's running, else the cloud
  • key       → your saved inferroute key
  • tag       → x-inferroute-client: cowork  (so the dashboard can attribute it)

Why this is cheap and stable:
  • Config-only — we do NOT fork goose. We own a small set of goose config/secret
    keys and re-assert them on every launch, so a goose update can't drift us out
    of sync (and goose's CLI is a pinned binary you update with `goose update`).
  • goose reads these from files, so the desktop app (launched from the menu) and
    the CLI both pick them up.

The desktop app is the point-and-click way to use InferRoute for everyday work —
research, writing, files — no terminal needed. The CLI is the same engine in the
terminal.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import config

GOOSE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "goose"
CONFIG_FILE = GOOSE_DIR / "config.yaml"
SECRETS_FILE = GOOSE_DIR / "secrets.yaml"
CLIENT_TAG = "cowork"
GOOSE_INSTALL_URL = "https://github.com/block/goose/releases/download/stable/download_cli.sh"
GOOSE_DESKTOP_DOWNLOAD = "https://block.github.io/goose/"


# ── small YAML helpers (PyYAML) ──────────────────────────────────────────────
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        # A malformed/locked file shouldn't crash the launcher — start fresh,
        # but don't clobber: only merge our keys in _write_merged below.
        return {}


def _write_merged(path: Path, updates: dict, *, secret: bool) -> None:
    """Merge ``updates`` into the YAML at ``path``, preserving the user's other
    keys. Secret files are written 0600."""
    import yaml

    data = _load_yaml(path)
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    if secret:
        os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    if secret:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ── resolution ───────────────────────────────────────────────────────────────
def _default_model() -> str:
    """A balanced model InferRoute serves. Mirrors the `ir` default agent model."""
    try:
        from . import models

        alias = models.get("kimi")
        if alias is not None:
            return alias.model_id
    except Exception:
        pass
    return "moonshotai/Kimi-K2.6-TEE"


def _anthropic_host(creds: config.Credentials) -> str:
    """Route through the on-device recorder daemon when it's up (records + tags
    the session, then forwards to the cloud), else talk to the cloud directly."""
    try:
        from . import launch

        return launch._recording_daemon_url() or creds.api_url
    except Exception:
        return creds.api_url


def _goose_cli() -> str | None:
    return shutil.which("goose") or next(
        (str(p) for p in [Path.home() / ".local" / "bin" / "goose"] if p.exists()), None
    )


def _goose_desktop() -> str | None:
    """Path to the goose Desktop binary for this platform, or None."""
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Goose.app/Contents/MacOS/Goose")]
    elif sys.platform.startswith("win"):
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            candidates = [Path(la) / "Programs" / "goose" / "Goose.exe"]
    else:  # linux
        candidates = [
            home / ".local" / "opt" / "goose" / "Goose",
            Path("/usr/lib/goose/Goose"),
        ]
        w = shutil.which("Goose") or shutil.which("goose-desktop")
        if w:
            candidates.insert(0, Path(w))
    return next((str(p) for p in candidates if p.exists()), None)


# ── configure (idempotent, re-asserted every launch) ─────────────────────────
def configure(creds: config.Credentials, model: str | None = None) -> str:
    """Write InferRoute's goose config + secrets. Returns the routing host."""
    model = model or _default_model()
    host = _anthropic_host(creds)

    _write_merged(
        CONFIG_FILE,
        {"GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": model, "ANTHROPIC_HOST": host},
        secret=False,
    )
    _write_merged(
        SECRETS_FILE,
        {
            "ANTHROPIC_API_KEY": creds.api_key,
            "ANTHROPIC_CUSTOM_HEADERS": {"x-inferroute-client": CLIENT_TAG},
        },
        secret=True,
    )
    # goose stores secrets in the OS keyring by default; mgld-style headless boxes
    # (and many Linux desktops) have no working keyring, so point goose at its file
    # secret store. Set it for the GUI session too (Linux), so a menu-launched
    # desktop also reads secrets.yaml.
    if not sys.platform.startswith("win") and sys.platform != "darwin":
        envd = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "environment.d"
        try:
            envd.mkdir(parents=True, exist_ok=True)
            (envd / "99-inferroute-goose.conf").write_text("GOOSE_DISABLE_KEYRING=true\n")
        except Exception:
            pass
    return host


def _launch_env() -> dict:
    env = dict(os.environ)
    env["GOOSE_DISABLE_KEYRING"] = "true"
    return env


def _install_goose_cli(assume_yes: bool) -> bool:
    if _goose_cli():
        return True
    print("\n  goose isn't installed yet.")
    if not assume_yes:
        resp = input("  Install the goose CLI now (open-source, ~from block/goose)? [Y/n] ").strip().lower()
        if resp in ("n", "no"):
            return False
    print("  Installing goose…")
    try:
        rc = subprocess.call(
            f"curl -fsSL {GOOSE_INSTALL_URL} | CONFIGURE=false bash",
            shell=True,
        )
    except Exception as e:  # pragma: no cover
        print(f"  ✗ install failed: {e}")
        return False
    if rc != 0 or not _goose_cli():
        print("  ✗ goose install didn't complete. Install it manually: https://block.github.io/goose/")
        return False
    return True


# ── commands ─────────────────────────────────────────────────────────────────
def setup_cowork() -> int:
    """Called from `ir setup`: install (if the user wants) + configure, no launch."""
    creds = config.load()
    if not creds.is_valid:
        print("  Skipping cowork — log in first (`ir login`).")
        return 0
    _install_goose_cli(assume_yes=False)
    host = configure(creds)
    routed = "the on-device recorder" if "localhost" in host else "InferRoute"
    print(f"\n  ✓ Cowork is wired to {routed}.")
    if _goose_desktop():
        print("      Launch the desktop app anytime, or run:  ir cowork")
    else:
        print(f"      Get the desktop app:  {GOOSE_DESKTOP_DOWNLOAD}")
        print("      Or use it in the terminal now:  ir cowork")
    return 0


def cmd_cowork(rest: list[str]) -> int:
    """`ir cowork [--cli] [--configure-only] [--model NAME] [-- <goose args>]`."""
    import argparse

    ap = argparse.ArgumentParser(prog="ir cowork", add_help=True)
    ap.add_argument("--cli", action="store_true", help="run the goose CLI even if the desktop is installed")
    ap.add_argument("--configure-only", action="store_true", help="wire goose to InferRoute, don't launch")
    ap.add_argument("--model", default=None, help="model to pin (default: kimi)")
    ns, passthrough = ap.parse_known_args(rest)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    creds = config.load()
    if not creds.is_valid:
        sys.stderr.write("\n  Not logged in. Run `ir login` (or `ir setup`) first.\n\n")
        return 2

    model = None
    if ns.model:
        try:
            from . import models

            alias = models.get(ns.model)
            model = alias.model_id if alias is not None else ns.model
        except Exception:
            model = ns.model

    host = configure(creds, model=model)

    if ns.configure_only:
        print(f"  ✓ goose wired to InferRoute ({host}).")
        return 0

    # Launch: prefer the desktop (the point-and-click experience) unless --cli.
    desktop = None if ns.cli else _goose_desktop()
    env = _launch_env()
    if desktop:
        print(f"  Launching goose desktop (InferRoute · {host})…")
        args = [desktop]
        if not (sys.platform == "darwin" or sys.platform.startswith("win")):
            args.append("--no-sandbox")  # chrome-sandbox isn't setuid in a user-local install
        try:
            subprocess.Popen(args, env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
        except Exception as e:
            print(f"  ✗ couldn't launch the desktop ({e}); falling back to the CLI.")

    goose = _goose_cli()
    if not goose:
        if not _install_goose_cli(assume_yes=False):
            return 1
        goose = _goose_cli()
    print(f"  Launching goose (InferRoute · {host})…")
    os.execvpe(goose, [goose, *passthrough], env)
    return 0  # never reached
