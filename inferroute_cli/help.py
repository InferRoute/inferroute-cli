"""`ir help` — one-screen explainer.

Plain stdout (no rich dependency) so it stays fast on cold-start.
"""

from __future__ import annotations

from . import models


def run(args=None) -> int:
    lines: list[str] = []
    lines.append("")
    lines.append("ir — Claude Code, routed through inferroute")
    lines.append("")
    lines.append("Daily use")
    for a in models.all_aliases():
        lines.append(a.help_line)
    lines.append("  ir anthropic   Skip inferroute. Plain Claude with your own setup.")
    lines.append("  ir choose      3-button picker — pick a model, then launch")
    lines.append("")
    lines.append("Account")
    lines.append("  ir login       Save your inferroute API key (one-time)")
    lines.append("  ir status      Personal usage + recent requests (TUI)")
    lines.append("")
    lines.append("How it works")
    lines.append("  Every command spawns `claude` as a subprocess and adds")
    lines.append("  --dangerously-skip-permissions so the agent runs without prompts.")
    lines.append("")
    lines.append("  The routed commands (ir, ir minimax, etc.) set ANTHROPIC_BASE_URL")
    lines.append("  to https://api.inferroute.ai and use your saved key — only for")
    lines.append("  that spawned process. Your shell stays untouched.")
    lines.append("")
    lines.append("  `ir anthropic` does NOT touch the env — it's pure pass-through.")
    lines.append("  Whatever your `claude` does normally, that's what happens.")
    lines.append("")
    lines.append("Where things live")
    lines.append("  ~/.config/inferroute/credentials   your API key (mode 600)")
    lines.append("")
    lines.append("Sign up: https://inferroute.ai")
    lines.append("Source:  https://github.com/InferRoute/inferroute-cli")
    lines.append("")
    print("\n".join(lines))
    return 0
