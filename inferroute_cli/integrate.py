"""`ir integrate-deferral-gate` — wire an existing autonomous-agent loop to
inferroute's economy (deferred / discounted) lane, using an agent.

Instead of handing the user docs, we launch Claude Code *in their repo*, pre-prompted
to find their loop(s), insert the economy gate at the right point, and set up per-role
model routing — with explicit confirmation and a backup before any file is changed.

Design: future-tasks-impl-plan.md §5b. The agent itself runs on a tier-2 model
(Kimi/GLM, the cheap backend) — onboarding dogfoods the product on first contact.
"""

from __future__ import annotations

from . import config, models
from .launch import launch_through_inferroute

# The model the integration agent runs on. Tier-2 (cheap) by deliberate choice —
# this is a bounded code task, and running onboarding on our own cheap backend
# is the dogfood. Override with `ir integrate-deferral-gate --model glm`.
DEFAULT_AGENT_MODEL = "kimi"


def _build_prompt(api_url: str) -> str:
    """The agent's standing instruction. `api_url` is the user's inferroute base
    URL; the agent reads the API key from the user's existing inferroute creds /
    env — we never paste the secret into the prompt."""
    return f"""\
You wire THIS repo's autonomous-agent loop to inferroute's economy lane (cheap off-peak runs).

HARD RULES:
- Use the installed `ir` CLI (it's on PATH — this agent was launched via it). Do NOT read or
  import inferroute's source / Python package to reverse-engineer it — `ir` already wraps the
  gate. Just call the command.
- Claude Code CANNOT set arbitrary HTTP headers. Do NOT search for a way to send an `IR-Lane`
  header — there is none. The economy lane is selected by the BASE URL (see Step 3).
- Time-box discovery: if you haven't found a loop after a few greps, report what you saw and stop.
- Never auto-commit. Show a diff and get y/n before writing any file. Keep responses short.

STEP 1 — Find the loop (grep, don't overthink):
The repeating driver: `run-loop.sh`, `while true`, cron, systemd `*.service`, or an existing
pace/gate line (e.g. `pace-gate`). Name the file + line. That line is the seam.

STEP 2 — Insert the gate (show the diff, don't write yet). Use the `ir gate` command — it owns
the poll + fail-open + jitter, exits 0=run / 1=skip (like `pace-gate`):
```bash
if ir gate; then <run one cycle>; else sleep 30; fi
```
If there's an existing `pace-gate` line, COMPOSE rather than replace:
`~/bin/pace-gate ... && ir gate && <run one cycle>`.
IMPORTANT runtime-PATH check: the loop may run under systemd/cron with a stripped PATH where
`ir` isn't visible. Resolve `command -v ir` now and use its ABSOLUTE path in the edit (e.g.
`/home/<user>/.local/bin/ir gate`). Only if `ir` cannot be guaranteed at the loop's runtime,
fall back to raw curl: `curl -fsS -m10 -H "Authorization: Bearer $IR_KEY" "$IR_API_URL/v1/gate"`
+ `jq -r .go`. Prefer `ir gate`.

STEP 3 — Make the discount apply (CRITICAL): the gated cycle's LLM calls must hit inferroute's
ECONOMY base URL. Trace how this repo's cycle reaches a model today (it may run native Claude
and even `unset ANTHROPIC_BASE_URL`). For gated cycles only, set the economy env — the easiest
correct way is `ir gate --print-env` (emits the two exports below), or set them directly:
```bash
export ANTHROPIC_BASE_URL="$IR_API_URL/economy"    # e.g. {api_url}/economy
export ANTHROPIC_AUTH_TOKEN="$IR_KEY"
```
The SDK appends `/v1/messages` → requests hit `/economy/v1/messages` → billed economy. State
plainly where in the cycle that env must be set. If the cycle can't be pointed at inferroute,
say so — don't pretend the discount works when it can't.

STEP 4 — Model routing: leave invocations on `auto` (inferroute's router picks the tier per
turn). If the loop has clear roles (manager/planner/executor/etc.), you MAY suggest per-role
tier hints (trivial→cheap, heavy→premium) as hints, not hard pins. Orthogonal to the gate.

STEP 5 — Apply: per file → show diff → y/n → write `<file>.bak` then edit → `bash -n` check.
Then one short summary: files changed (+.bak), how to test one gated cycle, how to revert.
"""


def cmd_integrate(args: list[str]) -> int:
    """Entry point for `ir integrate-deferral-gate`."""
    model = DEFAULT_AGENT_MODEL
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1

    creds = config.load()
    if not creds.is_valid:
        print("No inferroute API key found. Run `ir login` first.")
        return 2

    alias = models.get(model)
    if alias is None:
        print(f"Unknown model '{model}'. Try: kimi, glm, auto.")
        return 2

    prompt = _build_prompt(creds.api_url)
    print("⚡ Launching the inferroute integration agent (model: %s)…" % alias.short)
    print("   It will scan this repo, propose gate edits, and ask before writing.\n")
    # Interactive Claude Code session (NOT headless -p): the agent needs to ask for
    # y/n confirmation per file. The prompt is passed as the initial positional arg.
    # execvpe replaces the process, so this normally never returns.
    launch_through_inferroute(alias.model_id, creds, extra_args=[prompt, *rest])
    return 0
