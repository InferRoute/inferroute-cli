# The scrubber — local, deterministic secret redaction

`inferroute_local/scrubber.py` is a single, self-contained, **stdlib-only**
module (plus a thin CLI in `scrubber_cli.py`). It sanitizes logs / session data
**on the user's machine, before anything is synced to the cloud**, and can
**rehydrate** cloud results back to the real values locally.

It **never** opens a socket and **never** transmits anything itself. It only
rewrites text.

## Why it's not a dumb `***` masker

Three properties make the output useful to a downstream LLM while still hiding
every secret:

1. **Stable tokenization.** Each distinct secret *value* maps to a stable
   placeholder that encodes its kind and a short HMAC hash:

   ```
   <SECRET:stripe:a1f3c9>
   <SECRET:jwt:7f0b21>
   <SECRET:env:OPENAI_API_KEY:c2e0aa>
   ```

   The **same value always yields the same placeholder** — within a run and
   across runs (via a persistent local salt). So a cloud agent can still reason
   about correlation ("these 12 log lines share one key") without ever seeing
   the key. Collisions on the short hash are resolved by lengthening the hash
   until the placeholder uniquely identifies the value.

2. **Reversible locally, never remotely.** A local-only reverse map
   `{placeholder -> original}` is kept on disk at `0600` under the user's config
   dir. It is **never** sent anywhere. `rehydrate(text)` swaps placeholders back
   to the real values, for re-inflating cloud results before they're shown to
   the user.

3. **Structure-preserving.** Only the secret *value* is replaced. The key name,
   log shape, quotes, and punctuation around it are kept byte-for-byte:

   ```
   DATABASE_URL=postgres://app_user:<SECRET:uri:1972>@db.internal:5432/prod
   Authorization: Bearer <SECRET:bearer:3bde26>
   API_KEY=<SECRET:env:API_KEY:c2efa3>
   ```

## What it detects (v1, deterministic, no ML)

| Category | Examples |
|---|---|
| Known-format credentials | AWS `AKIA…/ASIA…`, OpenAI `sk-…`, Anthropic `sk-ant-…`, Stripe `sk_live_/pk_live_/rk_…`, GitHub `ghp_/gho_/…`, Slack `xox…`, Google `AIza…`, Google OAuth `ya29.…` |
| JWTs | three base64url segments (`eyJ….….…`) |
| Bearer / Authorization | `Authorization: Bearer …`, `proxy-authorization: token …` |
| PEM private keys | `-----BEGIN … PRIVATE KEY[ BLOCK]----- … -----END …` (RSA/DSA/EC/OPENSSH/PGP) |
| Provider secrets (contextual) | a known service keyword (airtable, datadog, mailchimp, …) next to a token value |
| Connection strings | `postgres://user:pass@…`, `redis://:pass@…`, `mongodb+srv://user:pass@…` — masks the password (and user if configured) |
| `.env`-style assignments | any `KEY` matching `SECRET\|TOKEN\|PASSWORD\|PASSWD\|API_KEY\|PRIVATE\|CREDENTIAL\|AUTH` |
| Generic high-entropy | unknown-format keys, gated on Shannon entropy + length + character-class diversity. In the ambiguous entropy band it skips natural-language identifiers (filenames, slugs, `camelCase`) so it doesn't corrupt non-secret context — ~70% fewer false positives on real code/docs, recall unchanged |
| PII *(opt-in, OFF by default)* | emails, IPv4/IPv6, phone numbers, Luhn-valid credit cards |

## CLI

```bash
# Sanitize a log to stdout (streaming, bounded memory — handles multi-GB files)
inferroute-scrub scrub app.log > app.scrubbed.log

# From stdin, with a value-free redaction report on stderr
some-cmd | inferroute-scrub scrub - --report > clean.log

# Preview exactly what WOULD be sent, as a diff, before anything leaves the box
inferroute-scrub scrub app.log --dry-run

# Re-inflate a cloud result locally (placeholders -> real values)
inferroute-scrub rehydrate cloud_reply.txt

# Opt into the PII layer (off by default)
inferroute-scrub --pii scrub runtime.log
```

## Library API

```python
from inferroute_local.scrubber import Scrubber

scr = Scrubber()                      # uses ~/.config/inferroute/scrubber
res = scr.scrub(payload_text)
res.scrubbed_text                     # safe to send
res.redaction_report                  # {"total", "distinct", "by_category"} — NO values
res.reverse_map_ref                   # path to the local 0600 map (never its contents)

restored = scr.rehydrate(cloud_reply) # placeholders -> originals, locally
```

`scrub(text) -> {scrubbed_text, redaction_report, reverse_map_ref}` and
`rehydrate(text) -> originals_restored` are the I/O contract. For huge inputs,
`scrub_stream(in_fp, out_fp)` reads line-by-line (buffering only multi-line PEM
blocks) so memory stays bounded.

## Configuration

`~/.config/inferroute/scrubber/config.json` (override the directory with
`INFERROUTE_SCRUBBER_DIR` or the `--config-dir` flag):

```json
{
  "pii": false,
  "contextual": true,
  "mask_userinfo": false,
  "hash_len": 6,
  "entropy_min_len": 20,
  "entropy_min_bits": 3.5,
  "entropy_min_classes": 2,
  "entropy_word_skip_ceiling": 4.2,
  "allowlist": ["never-redact-this-literal", "/never-redact-this-regex/"],
  "denylist": ["INTERNAL-[0-9]{4}", "custom-secret-shape-\\w+"]
}
```

- **allowlist** (never-redact): literal strings, or `/regex/` (slash-wrapped).
- **denylist** (always-redact): custom regexes. Denylist beats allowlist.
- **contextual** (default on): gitleaks-style — mask a token-shaped value when a
  known provider keyword (airtable, datadog, mailchimp, …; harvested from the
  gitleaks rule set) sits next to it. Catches short/numeric provider secrets the
  generic-entropy net misses. Gated to a token value ≥8 chars to bound false
  positives (~0.7% extra masks on real config trees).

## Fail-safe behaviour

- On uncertainty for a *secret* candidate, it **redacts** (favours
  false-positives over leaks).
- If the reverse map cannot be written securely (`0600`), it **refuses to run**
  (`ScrubberError`) rather than scrub without recoverability.
- **Idempotent:** re-scrubbing already-scrubbed text is a no-op — existing
  placeholders are protected and never re-redacted.

## Honesty / non-goals (read this)

- This is **best-effort secret-hiding, not anonymization and not a privacy
  guarantee.** It can miss secrets embedded in free text or in novel formats.
  Do **not** market it as "true privacy."
- **PII is OFF by default** on purpose. For the repo-scoped night-shift use case
  you mostly need to hide *secrets*; turning on PII redaction is a v2 concern
  that only matters once you expand beyond code into runtime logs. The PII layer
  that exists is intentionally simple (regex + Luhn), not a real anonymizer.
- **No model dependency in v1.** There is a clean `Detector` seam
  (`Callable[[str], Iterable[Match]]`) so a small local NER model can be plugged
  in later for fuzzy PII — optional, v2.

## Performance

Streaming, line-oriented, bounded memory. On a synthetic log with secrets every
8th line: **~11–13 MB/s single-threaded** (≈80–90 s per GB). Realistic logs with
sparser secrets run at the high end. Reproduce:

```bash
python3 tests/benchmark_scrubber.py --size-mb 256          # dense secrets
python3 tests/benchmark_scrubber.py --size-mb 256 --secret-every 50
```

Caveat: a single pathologically long line (e.g. a 1 GB single-line JSON blob)
is read whole by the line iterator. Logs are line-oriented in practice; a
chunked fallback for giant single lines is a possible v2 addition.

Overlap resolution is near-linear (O(k log k)) via interval clustering — disjoint
tokens form size-1 clusters accepted immediately; only the few intervals around
an actual secret are priority-resolved. A naive O(k²) scan blows up on data
files with tens of thousands of high-entropy tokens (a tokenizer `vocab.json`,
a CSV of ids); the clustered version scrubs 50k tokens in ~0.5 s.

## Corpus validation

Validated over the **entire `/home/henry/workspaces` tree, recursively**:
**110,043 UTF-8 text files / 2.5 GB**, with **100.0000%** of files passing both
invariants — `rehydrate(scrub(x)) == x` byte-exact and `scrub(scrub(x))` a no-op
— and 0 failures, with every detector (including the contextual one) enabled.
~79.5k files contained at least one masked secret. Idempotency is guaranteed by
construction: `scrub` iterates the single-pass transform to a fixed point, so
broad/overlapping detectors can never make a second pass drift. Reproduce:

```bash
python3 tests/validate_corpus.py /home/henry/workspaces
```

The run is read-only (reverse map goes to a throwaway temp dir). It also surfaced
and drove two fixes: the O(k²)→O(k log k) overlap clustering above, and a
structure fix so an inline `KEY='secret' command…` (e.g. a `PGPASSWORD='…' psql`
shell prefix) masks only the quoted value and keeps the trailing command.

## Recall benchmark (does it actually catch real secrets?)

The corpus validation proves self-consistency but not **recall** — the local
tree has no ground-truth labels. So `tests/recall_bench.py` measures recall
against the **gitleaks rule set** (221 battle-tested real-world secret regexes,
MIT): it generates randomized format-valid samples (via `exrex`), runs the
scrubber, and checks the secret value is masked by *any* detector.

    /tmp/scrubbench-venv/bin/python tests/recall_bench.py /tmp/gitleaks.toml

**Result: 97.5% recall** over ~1,000 sampled secrets. Adding the contextual
detector lifted this from 96.4% → 97.5% (+1.07 pts), closing provider formats
like airtable / asana / discord / dropbox / kucoin / linkedin that have no
distinctive prefix and are too short/numeric for the generic net. The residual
~2.5% are deep-tail short/numeric IDs (e.g. a 16-char hex `sidekiq` secret) and
the catch-all `generic-api-key`.

Cross-checked against a second, larger corpus — **secrets-patterns-db** (883
high-confidence patterns, ~4× gitleaks). That run surfaced two real bugs (now
fixed + regression-tested): PGP/DSA private-key blocks (`-----BEGIN PGP PRIVATE
KEY BLOCK-----`) slipped past the PEM detector, and Google OAuth `ya29.` tokens
had no rule. The residual misses on both corpora are deep-tail short/numeric
provider IDs (e.g. a 6-digit airbrake key) where masking would cost too much
structure to be worth it.

Bigger *labeled* corpora exist for pushing this further — **SecretBench** (97,479
labeled secrets / 15,084 true) and **FPSecretBench** (false positives from 9
tools) — but both are gated behind a data-use agreement (no open mirror exists),
so they're a follow-up once access is granted (the harness generalizes to any
labeled secret/label set).

## Tests

Stdlib `unittest` (no third-party deps), with a planted-secret fixture covering
every category:

```bash
python3 -m unittest tests.test_scrubber -v
```

Asserts 100% masking of planted secrets, exact structural preservation, stable
tokenization (same value → same placeholder, across runs), local round-trip
rehydration, idempotency, value-free reports, allow/deny lists, PII
off-by-default, `0600` map perms, and the fail-safe refusal.

## Integration hook (recorder)

The recorder's `full` level stores raw payloads in a content-addressed blob
store. The scrubber belongs at that single chokepoint — `Recorder._store(data)`
in `recorder.py` — so nothing un-scrubbed is ever persisted or synced:

```python
# in Recorder.__init__
from .scrubber import Scrubber
self._scrubber = Scrubber(config_dir=self.base_dir / "scrubber")

# in Recorder._store(self, data: bytes) -> str, before hashing/writing:
text = data.decode("utf-8", "replace")
data = self._scrubber.scrub(text).scrubbed_text.encode("utf-8")
```

This is documented rather than wired in by default, because content-addressed
dedup hashes change once payloads are scrubbed — enable it deliberately as part
of the recorder's privacy-level wiring, not as a silent behaviour change.
