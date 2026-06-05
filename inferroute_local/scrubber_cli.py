"""CLI for the local secret scrubber.

    inferroute-scrub scrub      [PATH|-]   # sanitize → stdout (streaming)
    inferroute-scrub rehydrate  [PATH|-]   # placeholders → originals → stdout
    inferroute-scrub scrub --dry-run FILE  # show the diff of what WOULD be sent

Nothing here touches the network. The reverse map stays local (0600).
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from .scrubber import Scrubber, ScrubberConfig, ScrubberError


def _open_input(path: str):
    if path == "-" or path == "":
        return sys.stdin
    return open(path, "r", encoding="utf-8", errors="replace")


def _build_scrubber(args) -> Scrubber:
    cfg = None
    if args.pii is not None:
        cfg = ScrubberConfig()
        cfg.pii = args.pii
    return Scrubber(config_dir=args.config_dir, config=cfg)


def _cmd_scrub(args) -> int:
    scr = _build_scrubber(args)

    if args.dry_run:
        # Preview: load the input, show exactly what would leave the machine.
        # (Dry-run intentionally materializes the input — it is for human
        # verification, not the streaming hot path.)
        with _open_input(args.path) as fp:
            original = fp.read()
        result = scr.scrub(original)
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            result.scrubbed_text.splitlines(keepends=True),
            fromfile="original (LOCAL ONLY)",
            tofile="would-be-sent (scrubbed)",
        )
        sys.stdout.writelines(diff)
        if not result.scrubbed_text.endswith("\n"):
            sys.stdout.write("\n")
        print(
            f"\n# redaction_report: {json.dumps(result.redaction_report)}",
            file=sys.stderr,
        )
        print(f"# reverse_map: {result.reverse_map_ref} (local, never sent)", file=sys.stderr)
        return 0

    with _open_input(args.path) as fp:
        report = scr.scrub_stream(fp, sys.stdout)
    if args.report:
        print(f"# redaction_report: {json.dumps(report)}", file=sys.stderr)
    return 0


def _cmd_rehydrate(args) -> int:
    scr = _build_scrubber(args)
    with _open_input(args.path) as fp:
        # Rehydration must see whole placeholders; they never span lines, so a
        # line-streaming pass is safe and keeps memory bounded.
        for line in fp:
            sys.stdout.write(scr.rehydrate(line))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="inferroute-scrub",
        description="Local, deterministic secret redaction. Never uses the network.",
    )
    p.add_argument(
        "--config-dir",
        default=None,
        help="Where salt + reverse map + config live (default: ~/.config/inferroute/scrubber).",
    )
    pii = p.add_mutually_exclusive_group()
    pii.add_argument("--pii", dest="pii", action="store_true", default=None,
                     help="Also redact PII (emails, IPs, phones, Luhn-valid cards). OFF by default.")
    pii.add_argument("--no-pii", dest="pii", action="store_false",
                     help="Force the PII layer off (the default).")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scrub", help="Sanitize text/logs → stdout.")
    sp.add_argument("path", nargs="?", default="-", help="Input file, or - for stdin.")
    sp.add_argument("--dry-run", action="store_true",
                    help="Print a diff of what WOULD be sent instead of the scrubbed stream.")
    sp.add_argument("--report", action="store_true",
                    help="Print the value-free redaction report to stderr.")
    sp.set_defaults(func=_cmd_scrub)

    rp = sub.add_parser("rehydrate", help="Swap placeholders back to originals → stdout.")
    rp.add_argument("path", nargs="?", default="-", help="Input file, or - for stdin.")
    rp.set_defaults(func=_cmd_rehydrate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ScrubberError as e:
        print(f"inferroute-scrub: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
