"""Map short `ir <name>` aliases to inferroute model IDs.

Keep this list small and meaningful — `ir minimax` is the contract; the
underlying model ID can change without breaking users' muscle memory.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelAlias:
    short: str           # what the user types: ir <short>
    model_id: str        # what we pass to claude --model
    label: str           # one-line description shown by `ir help` / `ir choose`
    tier: str            # "fast" | "balanced" | "smart"

    @property
    def help_line(self) -> str:
        return f"  ir {self.short:11s} {self.label}"


# Order matters — `ir choose` shows them top-to-bottom.
# NOTE: `minimax` currently points to the multi-model router because the
# direct `minimax/...` paths have a provider-mapping bug on the proxy
# (chutes matches MiniMaxAI/* patterns, not minimax/*; needs a normalizer
# pass). Router-routed requests get MiniMax M2.5 anyway for tier-2 tasks,
# so this is a transparent workaround. Revert to `minimax/minimax-m2.5`
# once the backend mapping is fixed.
ALIASES: list[ModelAlias] = [
    ModelAlias(
        short="auto",
        model_id="multi-model",
        label="Let inferroute pick the best model for the task",
        tier="balanced",
    ),
    ModelAlias(
        short="minimax",
        model_id="multi-model",  # TODO: revert to "minimax/minimax-m2.5" once backend mapping fixed
        label="MiniMax M2.5 — fast, cheap, great for most coding",
        tier="fast",
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
