"""`ir --resume` / `ir --continue` — resume a past conversation through inferroute.

`claude --resume` dumps every transcript in this directory into one
undifferentiated picker and, through ir, would tag the resumed turns under a NEW
inferroute session (new dashboard link, cost restarting at $0). ir does better:

  * Fresh ir launches force claude's session id to equal the inferroute id
    (launch.py `--session-id`), so a session has ONE id across the transcript,
    the dashboard link, and the cost file.
  * ir records which sessions it launched, with model/lane (launch.launch_index).

So this module lists the current directory's sessions, marks YOUR inferroute ones
(annotated with model · lane · cost-so-far) apart from plain-`claude` ones, lets
you pick, and resumes the chosen id cumulatively — same link, same climbing cost,
on the session's original model.

Resolution:
  * `ir --resume <id>` / `--resume=<id>` → resume that id.
  * `ir --continue` / `-c`              → resume the most recent session here.
  * `ir --resume` / `-r` (no id)        → open the menu.
Anything we can't resolve (no tty, no sessions, no remembered model) falls back to
plain `claude --resume` through inferroute on your last model (non-cumulative) or,
failing that, the model picker.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import launch


# ── transcript discovery ──────────────────────────────────────────────────────

def _project_dir(cwd: str | None = None) -> Path:
    """Claude Code keeps a directory's transcripts under
    ~/.claude/projects/<path-with-/-as->/. Mirror that mapping for the cwd."""
    p = cwd if cwd is not None else os.getcwd()
    return Path.home() / ".claude" / "projects" / p.replace("/", "-")


def _read_meta(path: Path) -> dict:
    """Pull title / branch / first-user-text / model from a transcript without
    reading the whole (possibly multi-MB) file — scan a bounded prefix."""
    title = branch = first_user = model = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                title = o.get("aiTitle") or title
                branch = branch or o.get("gitBranch")
                m = o.get("message")
                if isinstance(m, dict) and m.get("model"):
                    model = m.get("model")
                if first_user is None and o.get("type") == "user":
                    c = (o.get("message") or {}).get("content")
                    if isinstance(c, str):
                        first_user = c
                    elif isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "text":
                                first_user = b.get("text")
                                break
    except OSError:
        pass
    return {"title": title, "branch": branch, "first_user": first_user, "model": model}


def _cost(session_id: str) -> float | None:
    try:
        v = (Path.home() / ".inferroute" / "sessions" / f"{session_id}.cost").read_text().strip()
        return float(v)
    except (OSError, ValueError):
        return None


@dataclass
class Session:
    id: str
    title: str
    mtime: float
    branch: str | None
    is_ir: bool
    model: str | None
    lane: str | None
    cost_usd: float | None


def list_sessions(cwd: str | None = None, limit: int = 50) -> list[Session]:
    """Sessions for the cwd, inferroute ones first then native, each recency-desc."""
    d = _project_dir(cwd)
    if not d.is_dir():
        return []
    idx = launch.launch_index()
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: list[Session] = []
    for p in files[:limit]:
        sid = p.stem
        rec = idx.get(sid)
        meta = _read_meta(p)
        title = meta["title"]
        if not title and meta["first_user"]:
            title = meta["first_user"].strip().splitlines()[0] if meta["first_user"].strip() else None
        title = (title or "(untitled)")[:72]
        out.append(Session(
            id=sid,
            title=title,
            mtime=p.stat().st_mtime,
            branch=meta["branch"],
            is_ir=rec is not None,
            model=(rec or {}).get("model") or meta["model"],
            lane=(rec or {}).get("lane"),
            cost_usd=_cost(sid),
        ))
    # inferroute sessions grouped first (Henry: "see both but separate them"),
    # each group most-recent-first.
    out.sort(key=lambda s: (not s.is_ir, -s.mtime))
    return out


def newest(cwd: str | None = None) -> str | None:
    """Most recently active session id for the cwd (for `--continue` / `-c`)."""
    d = _project_dir(cwd)
    if not d.is_dir():
        return None
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime).stem


def _transcript_model(session_id: str, cwd: str | None = None) -> str | None:
    return _read_meta(_project_dir(cwd) / f"{session_id}.jsonl").get("model")


# ── argv parsing ──────────────────────────────────────────────────────────────

def _parse(args: list[str]) -> tuple[str, str | None, list[str]]:
    """(mode, explicit_id, rest). mode 'continue' for -c/--continue else 'menu';
    explicit_id from `--resume <id>` / `--resume=<id>`; rest = args minus the
    resume tokens so other passthrough flags survive."""
    mode = "menu"
    explicit_id: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--continue"):
            mode = "continue"
            i += 1
            continue
        if a.startswith("--resume="):
            explicit_id = a.split("=", 1)[1] or None
            i += 1
            continue
        if a in ("-r", "--resume"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                explicit_id = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        rest.append(a)
        i += 1
    return mode, explicit_id, rest


# ── menu ──────────────────────────────────────────────────────────────────────

def _ago(ts: float) -> str:
    d = max(0.0, time.time() - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _pick(sessions: list[Session]) -> str | None:
    """Full-screen picker; returns the chosen session id, or None if quit."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import ListItem, ListView, Static

    GREEN, INK, MUTE, DIM = "#37E59B", "#FAFAFA", "#8E8E98", "#5A5A63"

    def row(s: Session) -> str:
        if s.is_ir:
            bits = []
            from . import models
            short = models.short_for_model_id(s.model) if s.model else None
            bits.append(short or (s.model or "inferroute"))
            if s.lane:
                bits.append(s.lane)
            if s.cost_usd:
                bits.append(f"${s.cost_usd:.2f}")
            meta = "  ·  ".join(bits)
            line2 = f"[{GREEN}]⚡ {meta}[/]   [{MUTE}]{_ago(s.mtime)}[/]"
            if s.branch:
                line2 += f"   [{MUTE}]{s.branch}[/]"
            return f"[b {INK}]{s.title}[/]\n{line2}"
        line2 = f"[{DIM}]○ native claude[/]   [{MUTE}]{_ago(s.mtime)}[/]"
        if s.branch:
            line2 += f"   [{MUTE}]{s.branch}[/]"
        return f"[{INK}]{s.title}[/]\n{line2}"

    class ResumeApp(App):
        CSS = f"""
        Screen {{ align: center middle; background: #0A0A0C; }}
        #panel {{ width: 92; height: auto; max-height: 90%; padding: 1 2;
                  border: round #2A2A30; background: #101014; }}
        #head {{ width: 100%; color: {INK}; text-style: bold; padding-bottom: 1; }}
        ListView {{ height: auto; background: transparent; }}
        ListItem {{ padding: 0 2; margin-bottom: 1; background: #16161B; border: round #16161B; }}
        ListItem:hover {{ border: round #34343C; }}
        ListView > ListItem.-highlight {{ background: {GREEN} 12%; border: round {GREEN}; }}
        ListItem > Static {{ width: 100%; }}
        #hint {{ width: 100%; color: {MUTE}; padding-top: 1; }}
        """
        BINDINGS = [
            Binding("q", "quit", "quit"),
            Binding("escape", "quit", "quit"),
            Binding("enter", "select", "select"),
            Binding("up", "cursor_up", "up", show=False),
            Binding("down", "cursor_down", "down", show=False),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.chosen: str | None = None

        def compose(self) -> ComposeResult:
            n_ir = sum(1 for s in sessions if s.is_ir)
            with Vertical(id="panel"):
                yield Static(
                    f"[b {GREEN}]inferroute[/]  resume a session   "
                    f"[{MUTE}]· {n_ir} inferroute · {len(sessions) - n_ir} native[/]",
                    id="head",
                )
                items = [ListItem(Static(row(s)), id=f"s_{i}") for i, s in enumerate(sessions)]
                yield ListView(*items, id="picker")
                yield Static("[b]↑/↓[/] move   [b]enter[/] resume   [b]q[/] quit", id="hint")

        def on_mount(self) -> None:
            lv = self.query_one("#picker", ListView)
            lv.focus()
            lv.index = 0

        def action_cursor_up(self) -> None:
            self.query_one("#picker", ListView).action_cursor_up()

        def action_cursor_down(self) -> None:
            self.query_one("#picker", ListView).action_cursor_down()

        def action_select(self) -> None:
            lv = self.query_one("#picker", ListView)
            if lv.highlighted_child is not None:
                self._pick_index(lv.highlighted_child.id)

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            self._pick_index(event.item.id)

        def _pick_index(self, item_id: str | None) -> None:
            if item_id and item_id.startswith("s_"):
                self.chosen = sessions[int(item_id[2:])].id
            self.exit()

    app = ResumeApp()
    app.run()
    return app.chosen


# ── orchestration ─────────────────────────────────────────────────────────────

def _launch(model: str, extra_args: list[str], session_id: str | None) -> int:
    from .config import load

    creds = load()
    if not creds.is_valid:
        sys.stderr.write(
            "\n  No inferroute key configured.\n"
            "  Run `ir login` to set one up, or `ir help` for options.\n\n"
        )
        return 2
    launch.launch_through_inferroute(model, creds, extra_args=extra_args, session_id=session_id)
    return 0  # never reached — exec replaces process


def _fallback(passthrough: list[str]) -> int:
    """Resume via plain `claude --resume` through inferroute on the last model
    (non-cumulative), or drop to the model picker if no model is remembered."""
    model = launch.last_model()
    if model is not None:
        return _launch(model, passthrough, session_id=None)
    from . import choose as choose_mod
    return choose_mod.run(passthrough)


def handle(passthrough: list[str], model_override: str | None = None) -> int:
    """Resolve a resume target and launch it. `passthrough` is argv with `--model`
    already stripped; `model_override` is the model the user pinned, if any (it
    wins over the session's recorded model). Returns an exit code (won't return on
    a successful launch)."""
    mode, explicit_id, rest = _parse(passthrough)
    cwd = os.getcwd()

    target = explicit_id
    if target is None and mode == "continue":
        target = newest(cwd)
    elif target is None:  # menu
        if not sys.stdout.isatty():
            return _fallback(passthrough)  # no terminal → claude's own resume flow
        sessions = list_sessions(cwd)
        if not sessions:
            sys.stderr.write("\n  ir: no past sessions to resume in this directory.\n\n")
            return 1
        target = _pick(sessions)
        if target is None:
            return 130  # quit without choosing

    if not target:
        # `-c` with nothing to continue, or an unresolved target.
        return _fallback(passthrough)

    # Model: an explicit `--model` wins (resume but switch model); else the
    # session's own model — indexed inferroute model, else the transcript's —
    # falling back to the last-used model.
    rec = launch.launch_index().get(target)
    model = model_override or (rec or {}).get("model") or _transcript_model(target, cwd) or launch.last_model()
    if model is None:
        return _fallback(passthrough)

    # Tagging with `target` (== the session's inferroute id, for ir-origin
    # sessions) makes the resumed turns cumulative on the same dashboard + cost.
    return _launch(model, list(rest) + ["--resume", target], session_id=target)
