# ir — Claude Code through inferroute

Tiny launcher: one command (`ir`) spawns `claude` with the model you pick
and your inferroute key, so you don't have to set env vars every time.
**Your normal `claude` is never touched** — `ir` only configures the
subprocess it spawns.

You choose the model per session — there's no auto-routing. Optionally, an
on-device recorder logs your choices privately (see [Local recording](#local-recording-optional)).

## Install

```bash
pipx install inferroute
ir setup
```

That's it. `ir setup` logs you in (key saved to
`~/.config/inferroute/credentials`, mode 600) and enables private on-device
recording — **full by default**, since it never leaves your machine; change it
with `ir add recording --level minimal` or turn it off with `ir remove
recording`. Update any time with `pipx upgrade inferroute`.

Sign up for a key at https://inferroute.ai if you don't have one. (Prefer the
steps separately? `ir login` then `ir add recording` do the same thing.)

## Commands

```
ir setup            first-time onboarding: log in + optionally enable recording
ir                  open the model picker, then launch (same as `ir choose`)
ir --model NAME     pin a model — short alias or canonical id; any claude flag passes through
ir choose           interactive picker — pick a model, then launch
ir anthropic        escape hatch — plain Claude, your own setup
ir status           personal usage view (TUI)
ir login            save / refresh your inferroute API key
ir help             one-screen explainer

ir add recording    turn on private on-device recording (see below)
ir data show        what's been recorded (counts, models, size)
ir data export DIR  copy the metadata layer  ·  ir data wipe  delete it all
ir remove recording stop recording
```

`NAME` is a short alias (`minimax`, `minimax-m3`, `kimi`, `glm`) or any
canonical id (e.g. `claude-opus-4-8`), which passes through verbatim.

Every command bakes in `--dangerously-skip-permissions`, which is the
right default for an agentic workflow. If you want the prompts back,
just use plain `claude`.

## What does each command do under the hood?

```
ir --model NAME      →  ANTHROPIC_BASE_URL=<recorder daemon if running,         \\
                                            else https://api.inferroute.ai>      \\
                        ANTHROPIC_AUTH_TOKEN=$saved_key                          \\
                        exec claude --dangerously-skip-permissions              \\
                                    --model <resolved-id>
ir anthropic         →  exec claude --dangerously-skip-permissions
                        (no env mutation — pure pass-through)
```

The env vars are scoped to the spawned `claude` process. Your shell
doesn't see them. When the local recorder daemon is running it sits in front
of `api.inferroute.ai` — it records locally, then forwards there (the cloud
still does its own backend fallbacks).

## Local recording (optional)

`ir add recording` runs a small daemon on `localhost:5005` that records — **on
this machine only** — which model you pick per task and how the turn went, so
you (or a future personal router) can learn your preferences. It is **never
uploaded; we never see it.**

- Default is `full` — choices, outcomes, **and** prompt text. The prompt text is
  what actually lets it learn your preferences, and it never leaves this machine.
  Pick `minimal` at install for choices + outcomes only (no prompt text), which is
  lighter but can't train a personal router.
- Inspect it any time with `ir data show`, copy the metadata layer with
  `ir data export DIR` (never raw prompt text), or delete everything with
  `ir data wipe`.
- It lives under `~/.inferroute`. Remove the daemon with `ir remove recording`
  (`--purge` also deletes the recorded data).

## Where your config lives

| Path | Contents |
|---|---|
| `~/.config/inferroute/credentials` | Your inferroute API key + base URL (mode 600). Edit by hand or re-run `ir login`. |

## Privacy

The CLI talks to:
- `https://api.inferroute.ai` for your traffic (everything except `ir anthropic`)
- `https://inferroute.ai` only on `ir help` — to print the signup URL

It does **not** phone home, no telemetry. If you enable `ir add recording`,
that data is written **only** to `~/.inferroute` on your machine and is never
uploaded — inspect or delete it any time with `ir data show` / `ir data wipe`.
Whatever your `claude --model …` sends to whatever upstream is on you.

`ir anthropic` is literally `exec claude` plus one flag — the inferroute
service sees nothing.

## Source

https://github.com/InferRoute/inferroute

Issues / feature requests / PRs welcome.

## License

MIT — see [LICENSE](LICENSE).
