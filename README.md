# ir — Claude Code, routed through inferroute

Tiny launcher: one command (`ir`) spawns `claude` with the right model
and your inferroute key, so you don't have to set env vars every time.
**Your normal `claude` is never touched** — `ir` only configures the
subprocess it spawns.

## Install

```bash
pipx install inferroute
ir login
```

That's it. `ir login` saves your API key to `~/.config/inferroute/credentials`
(mode 600) and verifies it against the inferroute API.

Sign up for a key at https://inferroute.ai if you don't have one.

## Commands

```
ir                  auto-route through inferroute (default)
ir auto             same as `ir`
ir minimax          MiniMax M2.5 — fast, cheap
ir kimi             Kimi K2.6 — strong reasoning
ir glm              GLM-5.1 — solid general-purpose
ir anthropic        escape hatch — plain Claude, your own setup
ir choose           3-button picker — pick a model interactively
ir status           personal usage view (TUI)
ir login            save / refresh your inferroute API key
ir help             one-screen explainer
```

Every command bakes in `--dangerously-skip-permissions`, which is the
right default for an agentic workflow. If you want the prompts back,
just use plain `claude`.

## What does each command do under the hood?

```
ir <model-alias>     →  ANTHROPIC_BASE_URL=https://api.inferroute.ai \\
                        ANTHROPIC_API_KEY=$saved_key                  \\
                        exec claude --dangerously-skip-permissions    \\
                                    --model <resolved-id>
ir anthropic         →  exec claude --dangerously-skip-permissions
                        (no env mutation — pure pass-through)
```

The env vars are scoped to the spawned `claude` process. Your shell
doesn't see them.

## Where your config lives

| Path | Contents |
|---|---|
| `~/.config/inferroute/credentials` | Your inferroute API key + base URL (mode 600). Edit by hand or re-run `ir login`. |

## Privacy

The CLI talks to:
- `https://api.inferroute.ai` for routed traffic (everything except `ir anthropic`)
- `https://inferroute.ai` only on `ir help` — to print the signup URL

It does **not** phone home, no telemetry. Whatever your `claude --model …`
sends to whatever upstream is on you.

`ir anthropic` is literally `exec claude` plus one flag — the inferroute
service sees nothing.

## License

MIT — see [LICENSE](LICENSE).
