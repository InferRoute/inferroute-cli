# Cowork — a desktop app for InferRoute, powered by goose

`ir` routes **Claude Code** through InferRoute. **Cowork** is the equivalent for
everyday, non-terminal work: research, writing, working with files — a
point-and-click app, no terminal needed.

It's powered by [**goose**](https://github.com/block/goose) (Block / Linux
Foundation, Apache-2.0), an open-source agent with both a **desktop app** and a
**CLI**. We don't fork it — `ir cowork` just wires goose to InferRoute and
launches it. Because the wiring is config-only and re-asserted on every launch,
goose updates can't drift it out of sync.

## Fastest path

```bash
ir cowork
```

This will:
1. install the goose CLI if it isn't already present (or you can grab the
   desktop app from <https://block.github.io/goose/>);
2. point goose at InferRoute — provider `anthropic`, your saved key, and routing
   through your on-device recorder daemon if it's running (`ir add recording`),
   otherwise straight to the cloud;
3. launch the **desktop app** if it's installed, otherwise the goose CLI.

You'll also be offered Cowork at the end of `ir setup`.

| command | what it does |
|---|---|
| `ir cowork` | wire goose to InferRoute and launch it |
| `ir cowork --cli` | force the goose CLI even if the desktop is installed |
| `ir cowork --configure-only` | wire it up, don't launch |
| `ir cowork --model NAME` | pin a model (short alias like `kimi`/`glm`, or a canonical id) |

## In the app

When the desktop app opens, pick a model from the list (populated from InferRoute's
`/v1/models`) and start a session. The same `inf_…` key you use with `ir` works
here — Cowork reads it from goose's local secret store, which `ir cowork` writes.

## What gets configured (and why it's safe)

`ir cowork` owns a small, stable set of keys and **merges** them into goose's
files without touching your other goose settings:

- `~/.config/goose/config.yaml` — `GOOSE_PROVIDER: anthropic`,
  `GOOSE_MODEL`, `ANTHROPIC_HOST` (your recorder daemon or `api.inferroute.ai`)
- `~/.config/goose/secrets.yaml` (mode 600) — `ANTHROPIC_API_KEY` and a
  `x-inferroute-client: cowork` tag so the dashboard can attribute Cowork traffic
- `~/.config/environment.d/99-inferroute-goose.conf` (Linux) —
  `GOOSE_DISABLE_KEYRING=true`, so goose uses its file secret store on machines
  without a working OS keyring

These are re-written every time you run `ir cowork`, so they self-heal across
goose updates.

## Wiring it by hand

If you'd rather not use `ir cowork`, point any goose install at InferRoute:

```yaml
# ~/.config/goose/config.yaml
GOOSE_PROVIDER: anthropic
GOOSE_MODEL: moonshotai/Kimi-K2.6-TEE      # any model from /v1/models
ANTHROPIC_HOST: https://api.inferroute.ai  # or http://localhost:5005 to record locally
```

```yaml
# ~/.config/goose/secrets.yaml   (chmod 600)
ANTHROPIC_API_KEY: inf_your_key_here
```

On Linux, also set `GOOSE_DISABLE_KEYRING=true` (e.g. in
`~/.config/environment.d/`) if your machine has no working keyring.

## Notes

- **Updates:** the goose CLI is a pinned binary — update it on your schedule with
  `goose update`. The desktop app auto-updates; `ir cowork` re-asserts the config
  each launch, so that's handled.
- **Cost display:** goose's in-app cost readout depends on
  [block/goose#9719](https://github.com/block/goose/pull/9719); until it merges,
  cost may show as "unavailable" even though routing and billing work normally —
  see your real spend with `ir status` or on the dashboard.
