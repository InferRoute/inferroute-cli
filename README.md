# ir — Claude Code through inferroute

Tiny launcher: one command (`ir`) spawns `claude` with the model you pick
and your inferroute key, so you don't have to set env vars every time.
**Your normal `claude` is never touched** — `ir` only configures the
subprocess it spawns.

You choose the model per session — there's no auto-routing. Optionally, an
on-device recorder logs your choices privately (see [Local recording](#local-recording-optional)).

## Install

```bash
pipx install git+https://github.com/InferRoute/inferroute.git
ir login
```

That's it. `ir login` saves your API key to `~/.config/inferroute/credentials`
(mode 600) and verifies it against the inferroute API.

Sign up for a key at https://inferroute.ai if you don't have one.

> _A `pipx install inferroute` PyPI package is coming once the API surface
> settles. For now, install straight from GitHub — pipx clones the repo
> into its own venv, so updates are a one-line `pipx upgrade inferroute`._

## Commands

```
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

- Default level is metadata only (model choices + outcomes, no prompt text);
  `full` also stores raw payloads locally.
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
