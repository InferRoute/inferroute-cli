"""`ir choose` — interactive model picker.

Saves no state; just prints the `ir --model NAME` command to run, so the user
learns what to type next time, then launches it.

One screen: Fast (MiniMax), the flagship, the two balanced-tier models
(Kimi and GLM), and the native-Anthropic escape hatch. There is no auto-route —
the user always picks a concrete model; the local daemon never decides.

Each row shows the model's published price (USD per 1M tokens) across the three
buckets the dashboard uses — input / cache / output — so the user can weigh cost
at the point of choosing. Prices come from models.ALIASES (single source of
truth); the native-Anthropic row has none because it bills to the user's own
Claude plan.

Visual style mirrors inferroute.ai: near-black canvas, white text, and the v2
brand identity in the header — the "cost-down" logo mark (a routing node that
steps down to a price floor) and the two-tone lowercase wordmark, `infer` +
`route`. Per the brand rules (inferroute-site/public/brand/README.md) the logo
is white on a logged-out surface and brand green (#37E59B) when logged in.
"""

from __future__ import annotations

import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ListItem, ListView, Static

from . import models


# ── Brand palette (from inferroute-site globals.css + brand/README.md) ────────
_GREEN = "#37E59B"   # brand green — the v2 "save / cheap / go" accent
_BLUE = "#247FFF"    # --primary (electric blue, used for the selection accent)
_INK = "#FAFAFA"     # --foreground (near-white text)
_MUTE = "#8E8E98"    # --muted-foreground
_AMBER = "#FE9A00"   # chart-3
_VIOLET = "#AD46FF"  # chart-4

# "Cost-down" logo mark — a routing node (●) that steps down to a price floor
# (public/brand/mark.svg). Heavy box-drawing (━ ┓ ┗) approximates the SVG's
# thick stroke-width:6; Unicode has no heavy *rounded* corners, so the steps
# read as square here.
_MARK = "●━━┓\n   ┗━━"

# Sentinel id for the native-Anthropic escape hatch. Not a model alias —
# `ir anthropic` runs plain `claude` with no inferroute routing, so it's
# handled specially in run() rather than via models.get().
_ANTHROPIC = "anthropic"

# (option id, tier badge, accent color, display name, one-line description),
# in display order. Every id except _ANTHROPIC is a real alias short from
# models.ALIASES — its price is pulled from there.
_OPTIONS = [
    ("minimax",    "FAST",      _AMBER,  "MiniMax M2.7",     "get something usable — cheap, fast iteration"),
    ("minimax-m3", "FLAGSHIP",  _BLUE,   "MiniMax M3",       "newer MiniMax — multimodal, 1M context, fast"),
    ("kimi",       "BALANCED",  _GREEN,  "Kimi K2.6",        "strong reasoning, thinks before acting"),
    ("glm",        "BALANCED",  _GREEN,  "GLM-5.1",          "solid general-purpose alternative"),
    (_ANTHROPIC,   "ANTHROPIC", _VIOLET, "Native Anthropic", "your own Claude subscription — no routing"),
]


def _price_markup(short: str) -> str:
    """Rich-markup price line for an option, columns aligned across rows.

    Renders `in $0.18   cache $0.036   out $0.90  · per 1M tokens` for routed
    models, or a plain note for the native-Anthropic escape hatch (which has no
    inferroute price — it bills to the user's own Claude plan).
    """
    if short == _ANTHROPIC:
        return f"[italic {_MUTE}]billed to your own Claude plan · inferroute adds no charge[/]"

    alias = models.get(short)
    p = alias.price if alias else None
    if p is None:
        return f"[italic {_MUTE}]price not published[/]"

    # Pad each value to a fixed width so the columns line up down the list.
    def col(label: str, value: float, prec: int) -> str:
        v = f"${value:.{prec}f}"
        return f"[{_MUTE}]{label}[/] [b {_INK}]{v:<6}[/]"

    return (
        f"{col('in', p.input, 2)}  "
        f"{col('cache', p.cache_read, 3)}  "
        f"{col('out', p.output, 2)}"
        f"  [{_MUTE}]· per 1M tokens[/]"
    )


def _row_markup(badge: str, color: str, name: str, desc: str, price: str) -> str:
    """Three-line card body: badge + name, description, price."""
    return (
        f"[b {color}]●[/] [b {color}]{badge:<9}[/]  [b {_INK}]{name}[/]\n"
        f"[{_MUTE}]{desc}[/]\n"
        f"{price}"
    )


class ChooseApp(App):
    """Tiny model picker. The chosen option's id lands in .selected_short."""

    CSS = f"""
    Screen {{ align: center middle; background: #0A0A0C; }}

    #panel {{
        width: 82;
        height: auto;
        padding: 1 2;
        border: round #2A2A30;
        background: #101014;
    }}

    /* ── Header: logo mark + wordmark + tagline ───────────────────────── */
    #header {{ height: auto; padding-bottom: 1; }}
    #mark {{
        width: auto;
        text-style: bold;
        padding: 0 2 0 0;
    }}
    #title {{ width: 1fr; height: auto; }}
    #wordmark {{ width: 100%; color: {_INK}; text-style: bold; }}
    #tagline {{ width: 100%; color: {_MUTE}; }}

    #divider {{ width: 100%; color: #2A2A30; margin-bottom: 1; }}

    /* The list itself: transparent so cards read as rows. */
    ListView {{ height: auto; background: transparent; }}

    /* Every row carries a same-as-background border so it reserves the frame's
       space — only the COLOR changes on highlight, so there's no layout jitter
       as the cursor moves. The selected row pops: electric-blue frame + a blue
       wash, unmistakable against the calm resting rows. */
    ListItem {{
        padding: 0 2;
        margin-bottom: 1;
        background: #16161B;
        border: round #16161B;
    }}
    ListItem:hover {{ border: round #34343C; }}
    ListView > ListItem.-highlight {{
        background: {_BLUE} 15%;
        border: round {_BLUE};
    }}
    ListItem > Static {{ width: 100%; }}

    #hint {{ width: 100%; color: {_MUTE}; padding-top: 1; }}
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit"),
        Binding("enter", "select", "select", show=True),
        Binding("up", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_short: str | None = None

    def compose(self) -> ComposeResult:
        # Brand rule: logo is white on a logged-out surface, brand green when
        # logged in (a saved, valid inferroute key). The wordmark goes two-tone
        # — `infer` in ink + `route` in green — only in the green context.
        try:
            from .config import load
            logged_in = load().is_valid
        except Exception:
            logged_in = False
        mark_color = _GREEN if logged_in else _INK
        if logged_in:
            wordmark = f"[b {_INK}]infer[/][b {_GREEN}]route[/]"
        else:
            wordmark = f"[b {_INK}]inferroute[/]"

        with Vertical(id="panel"):
            with Horizontal(id="header"):
                yield Static(f"[b {mark_color}]{_MARK}[/]", id="mark")
                with Vertical(id="title"):
                    yield Static(wordmark, id="wordmark")
                    yield Static("choose a model · USD per 1M tokens", id="tagline")
            yield Static("─" * 76, id="divider")
            items = []
            for short, badge, color, name, desc in _OPTIONS:
                body = _row_markup(badge, color, name, desc, _price_markup(short))
                items.append(ListItem(Static(body), id=short))
            yield ListView(*items, id="picker")
            yield Static("[b]↑/↓[/] move    [b]enter[/] select    [b]q[/] quit", id="hint")

    def on_mount(self) -> None:
        # Focus the list so arrow keys / Enter work immediately, cursor on row 0.
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
            self._choose(lv.highlighted_child.id)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Fires on Enter or mouse click on a row.
        self._choose(event.item.id)

    def _choose(self, short: str | None) -> None:
        # ListItem ids are alias shorts (or the _ANTHROPIC sentinel).
        self.selected_short = short
        self.exit()


def run(extra_args=None) -> int:
    extra_args = list(extra_args or [])

    # The picker is a full-screen TUI — it needs a real terminal. Bail clearly
    # rather than crash/hang when stdout isn't a TTY (pipes, CI, or an agent's
    # Bash tool / a nested Claude Code session). Direct `ir --model NAME` still
    # works in those contexts.
    if not sys.stdout.isatty():
        sys.stderr.write(
            "\n  ir: the interactive picker needs a terminal.\n"
            "  Use `ir --model NAME` (e.g. `ir --model minimax`) instead.\n\n"
        )
        return 2

    app = ChooseApp()
    app.run()
    short = app.selected_short
    if short is None:
        return 130  # user quit without choosing

    # rich ships with textual; render markup so [bold] doesn't print literally.
    from rich.console import Console

    console = Console()
    console.print()
    console.print("  Run this next time directly:")
    if short == _ANTHROPIC:
        console.print("    [bold]ir anthropic[/bold]")
    else:
        console.print(f"    [bold]ir --model {short}[/bold]")
    console.print()

    # Spawn it now so the picker isn't a dead-end.
    from .config import load
    from .launch import launch_native_anthropic, launch_through_inferroute

    if short == _ANTHROPIC:
        launch_native_anthropic(extra_args)
        return 0  # never reached — exec replaces process

    alias = models.get(short)
    if alias is None:  # defensive — every other id is a real alias
        sys.stderr.write(f"  internal error: alias '{short}' missing\n")
        return 1
    launch_through_inferroute(alias.model_id, load(), extra_args=extra_args)
    return 0  # never reached — exec replaces process
