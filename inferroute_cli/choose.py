"""`ir choose` — interactive 3-button picker. Saves no state; just prints
the `ir <alias>` command to run, so the user learns what to type next time.
"""

from __future__ import annotations

import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Button, Footer, Static

from . import models


_TIER_PROMPTS = {
    "fast": "[b cyan]Fast[/b cyan]  — get something usable, cheap, fast iteration",
    "balanced": "[b green]Balanced[/b green] — reasonable smarts, normal coding tasks",
    "smart": "[b yellow]Smart[/b yellow]  — Let the router pick from all available models",
}


class ChooseApp(App):
    """Tiny 3-button picker. Returns the selected alias via .selected."""

    CSS = """
    Screen { align: center middle; background: $background; }
    #panel { width: 70; padding: 1 2; border: round $primary; }
    Static { width: 100%; }
    Button { width: 100%; margin-top: 1; }
    #q-prompt { padding-bottom: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected: models.ModelAlias | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static("[b]What kind of task?[/b]", id="q-prompt")
            yield Static(_TIER_PROMPTS["fast"])
            yield Button("Fast — MiniMax", id="fast", variant="primary")
            yield Static(_TIER_PROMPTS["balanced"])
            yield Button("Balanced — Kimi K2.6", id="balanced", variant="success")
            yield Static(_TIER_PROMPTS["smart"])
            yield Button("Smart — Auto-route", id="smart", variant="warning")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice_map = {
            "fast": "minimax",
            "balanced": "kimi",
            "smart": "auto",
        }
        short = choice_map.get(event.button.id)
        if short:
            self.selected = models.get(short)
        self.exit()


def run(args=None) -> int:
    app = ChooseApp()
    app.run()
    if app.selected is None:
        return 130  # user quit without choosing
    print()
    print(f"  Run this next time directly:")
    print(f"    [bold]ir {app.selected.short}[/bold]")
    print()
    # And spawn it now so the picker isn't a dead-end
    from .launch import launch_through_inferroute
    from .config import load
    launch_through_inferroute(app.selected.model_id, load())
    return 0  # never reached — exec replaces process
