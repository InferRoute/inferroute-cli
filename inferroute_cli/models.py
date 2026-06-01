"""Friendly name → canonical inferroute model ID.

Used by `ir --model NAME` (and by the interactive `ir choose` picker) to let
users type `minimax` instead of `MiniMax-M2.7`. The short name is the user-
facing contract; the canonical model_id can change without breaking muscle
memory.

These are NOT subcommands. The supported forms are:
    ir                              # auto (multi-model)
    ir --model minimax              # short name → translated
    ir --model MiniMax-M2.7         # canonical id passes through
    ir --model claude-opus-4-8      # any other model id passes through too
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelAlias:
    short: str           # what the user types as the --model value
    model_id: str        # what we pass to claude --model on the wire
    label: str           # one-line description shown by `ir help` / `ir choose`
    tier: str            # "fast" | "balanced" | "smart"

    @property
    def help_line(self) -> str:
        # Kept for `ir choose` button labels; `ir help` formats its own table.
        return f"  ir --model {self.short:<8} {self.label}"


# Order matters — `ir choose` shows them top-to-bottom.
ALIASES: list[ModelAlias] = [
    ModelAlias(
        short="auto",
        model_id="multi-model",
        label="Let inferroute pick the best model for the task",
        tier="balanced",
    ),
    ModelAlias(
        short="minimax",
        # Bare `minimax` stays pinned to M2.7 for backward-compat (muscle
        # memory). Pass the short name so Claude Code displays "MiniMax-M2.7"
        # in the session header instead of revealing "minimax_direct/…". Proxy
        # has a top-level `MiniMax-M2.7` model entry that routes to the
        # MiniMax Token Plan sub (api.minimax.io/anthropic).
        model_id="MiniMax-M2.7",
        label="MiniMax M2.7 (cheaper)",
        tier="fast",
    ),
    ModelAlias(
        short="minimax-m2.7",
        # Explicit M2.7 alias (same target as bare `minimax`).
        model_id="MiniMax-M2.7",
        label="MiniMax M2.7 — cheaper/smaller direct-sub model",
        tier="fast",
    ),
    ModelAlias(
        short="minimax-m3",
        # MiniMax M3 — newer flagship on the same direct Token Plan sub.
        # Proxy has a top-level `MiniMax-M3` entry routing to api.minimax.io/anthropic.
        model_id="MiniMax-M3",
        label="MiniMax M3 — newer/stronger flagship",
        tier="balanced",
    ),
    ModelAlias(
        short="kimi",
        model_id="moonshotai/Kimi-K2.6-TEE",
        label="Kimi K2.6 — strong reasoning, thinks before acting",
        tier="balanced",
    ),
    ModelAlias(
        short="glm",
        model_id="zai-org/GLM-5.1-TEE",
        label="GLM-5.1 — solid general-purpose alternative",
        tier="balanced",
    ),
]


def get(short: str) -> ModelAlias | None:
    short = short.lower().strip()
    for a in ALIASES:
        if a.short == short:
            return a
    return None


def all_aliases() -> list[ModelAlias]:
    return list(ALIASES)


def by_tier(tier: str) -> list[ModelAlias]:
    return [a for a in ALIASES if a.tier == tier]
