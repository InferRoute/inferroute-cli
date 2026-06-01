"""`ir choose` — interactive picker. Saves no state; just prints the
`ir --model NAME` command to run, so the user learns what to type next time.

One screen, five options: Fast (MiniMax), the two balanced-tier models
(Kimi and GLM), Smart (auto-route), and the native-Anthropic escape hatch.
"""

from __future__ import annotations

import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, Footer, Static

from . import models


# Sentinel id for the native-Anthropic escape hatch. Not a model alias —
# `ir anthropic` runs plain `claude` with no inferroute routing, so it's
# handled specially in run() rather than via models.get().
_ANTHROPIC = "anthropic"

# (button id, prompt line, button label, variant), in display order.
# Every id except _ANTHROPIC is a real alias short from models.ALIASES.
_OPTIONS = [
    (
        "minimax",
        "[b cyan]Fast[/b cyan]  — get something usable, cheap, fast iteration",
        "Fast — MiniMax",
        "primary",
    ),
    (
        "kimi",
        "[b green]Balanced[/b green] — strong reasoning, thinks before acting",
        "Balanced — Kimi K2.6",
        "success",
    ),
    (
        "glm",
        "[b green]Balanced[/b green] — solid general-purpose alternative",
        "Balanced — GLM-5.1",
        "success",
    ),
    (
        "auto",
        "[b yellow]Smart[/b yellow]  — let the router pick from all available models",
        "Smart — Auto-route",
        "warning",
    ),
    (
        _ANTHROPIC,
        "[b magenta]Anthropic[/b magenta] — your own Claude subscription, no routing",
        "Native Anthropic",
        "default",
    ),
]


class ChooseApp(App):
    """Tiny model picker. The chosen option's id lands in .selected_short."""

    CSS = """
    Screen { align: center middle; background: $background; }
    #panel { width: 70; padding: 1 2; border: round $primary; }
    Static { width: 100%; }
    #q-prompt { padding-bottom: 1; }
    /* Each option is a title tight against its button, with the gap
       ABOVE the group so the title clearly belongs to the button below. */
    .opt { height: auto; margin-top: 1; }
    .opt Button { width: 100%; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit"),
        Binding("up", "focus_previous", "up", show=False),
        Binding("down", "focus_next", "down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_short: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("[b]Which model?[/b]", id="q-prompt")
            for short, prompt, label, variant in _OPTIONS:
                with Vertical(classes="opt"):
                    yield Static(prompt)
                    yield Button(label, id=short, variant=variant)
        yield Footer()

    def on_mount(self) -> None:
        # Focus the first button so arrow keys / Enter work immediately.
        self.query(Button).first().focus()

    def action_focus_next(self) -> None:
        self.screen.focus_next(Button)

    def action_focus_previous(self) -> None:
        self.screen.focus_previous(Button)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Button ids are alias shorts (or the _ANTHROPIC sentinel).
        self.selected_short = event.button.id
        self.exit()


def run(args=None) -> int:
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
        launch_native_anthropic()
        return 0  # never reached — exec replaces process

    alias = models.get(short)
    if alias is None:  # defensive — every other id is a real alias
        sys.stderr.write(f"  internal error: alias '{short}' missing\n")
        return 1
    launch_through_inferroute(alias.model_id, load())
    return 0  # never reached — exec replaces process
