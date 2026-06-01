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
Wire THIS repo's autonomous-agent loop (if it has one) to inferroute's economy lane (cheap
off-peak runs). You're in plan mode: research, then present ONE concise plan and stop. Be
minimal and concrete.

The `ir` CLI is installed (you were launched via it). Use it directly; do NOT read inferroute's
source. For RESEARCH you may ONLY run: `ir help`, `ir gate`, `ir gate --print-env`. NEVER run
bare `ir` or `ir --model …` — those LAUNCH an agent session (a nested agent); they belong in the
loop you edit, not your research. Two ir commands do everything:
  • `ir gate`        → exit 0 = cheap window now, 1 = skip. Gates one cycle.
  • `ir --economy`   → launches an agent on the economy lane; inferroute auto-routes the model
                      per turn and sets ANTHROPIC_BASE_URL/token itself. Use it to REPLACE however
                      the loop launches its agent today. If it pins a model (e.g. `--model <name>`),
                      DROP the pin (pinned native models aren't on the economy backend) and let `ir`
                      route; keep flags like --effort. For a specific tier: `ir --economy --model kimi`
                      (or glm). (`ir --economy` == the `IR_LANE=economy ir …` env form, but cleaner.)

Plan to:
1. Find the loop: a recurring, UNATTENDED driver that repeatedly invokes an LLM-agent CLI
   (commonly `claude`, or an SDK runner). It may be a shell while/for loop, a cron entry, a
   systemd service/timer, a Makefile/npm/justfile target, a Python/Node runner, etc. — discover
   it, don't assume filenames. Note the file + line and how it launches the agent today.
   IF THERE IS NO SUCH LOOP: do NOT invent one, do NOT edit anything, and do NOT submit a plan.
   Just reply with the single line "No autonomous-agent loop found in this repo — nothing to
   integrate." and stop.
2. Gate each cycle: run `ir gate` before the agent invocation and skip the cycle when it returns
   non-zero (using the loop's own idiom — sleep+continue, etc.). Use ir's absolute path
   (`command -v ir`) if the loop runs with a stripped PATH (systemd/cron). Compose with any
   existing gate/throttle rather than replacing it.
3. Run the cycle on economy: replace the agent launch with `ir --economy …`, dropping any
   `--model` pin and any hand-rolled ANTHROPIC_BASE_URL/token env — `ir` handles all of it.
   (Per-role tiers via `ir --economy --model kimi`/`glm` only if the loop clearly separates roles.)
4. Present the plan: exact files + lines + edits, how to test one gated cycle, how to revert.
   On approval, apply the edits and syntax-check each changed file.
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
    print("   Plan mode: it scans this repo, proposes a plan, and waits for your approval.\n")
    # Launch in PLAN MODE with the research tools PRE-APPROVED so the user is never
    # interrupted with permission prompts on the way to the plan. Read-only + the two
    # safe `ir` research commands only (NOT bare `ir`/`ir auto`, which would launch a
    # nested session — also blocked by the CLAUDECODE guard in launch.py). The prompt
    # is the first positional; --allowedTools is variadic so it goes last.
    launch_through_inferroute(
        alias.model_id, creds,
        extra_args=[
            prompt, *rest,
            # Block subagent spawning — it's a tiny task; Explore/Task subagents
            # burn minutes + tokens and bloat context (slows Kimi). Grep+read directly.
            "--disallowedTools", "Task",
            "--allowedTools", *_RESEARCH_TOOLS,
        ],
        permission_mode="plan",
    )
    return 0


# Tools pre-approved for the planning phase so the user is NEVER prompted on the way to the
# plan. Broad `Bash` is deliberate: agents shell with compound commands (`a; echo $?`, `&&`)
# and varied discovery tools (crontab, systemctl, find…) that a granular allow-list can't
# cover without prompting. Safety holds via THREE other guards, not via restricting research:
#   1. --permission-mode plan  → file edits require explicit plan approval (the real gate).
#   2. --disallowedTools Task  → no Explore/subagents (keeps it fast + small-context).
#   3. CLAUDECODE guard in launch.py → bare `ir`/`ir --model …` can't spawn a nested session.
_RESEARCH_TOOLS = ["Read", "Grep", "Glob", "Bash"]
