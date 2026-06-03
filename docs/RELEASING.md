# Releasing inferroute

Two artifact families need to ship together for a clean release: the Python
package (PyPI) and the classifier model bundle (GitHub Releases). The CLI's
default `classifier_bootstrap_url` points at the GitHub release's "latest"
download path, so users running `ir add local-routing` get whichever model
was attached to the most recent tag.

## 1. Cut the Python release

```bash
# Bump the version in pyproject.toml, commit, tag
vim pyproject.toml                            # e.g. 0.1.0 → 0.2.0
git commit -am "release v0.2.0"
git tag v0.2.0
git push origin main --tags

# Build wheels
rm -rf dist/
python -m build

# Upload to PyPI (uses your ~/.pypirc or twine env vars)
python -m twine upload dist/*
```

## 2. Build the classifier bundle

The bundle is the contents of `~/.inferroute/models/classifier-v0/` packed
as individual files + a `classifier-v0-manifest.json` describing them.

```bash
# From inferroute-local-experiments — the canonical source of the trained model
cd ~/workspaces/inferroute/inferroute-local-experiments
MODEL_DIR=out/classifier-v0-longer
BUNDLE_DIR=/tmp/classifier-v0-bundle-$(date +%Y%m%d)
mkdir -p $BUNDLE_DIR/onnx

# Copy the four required files (and their data sidecar if present)
cp $MODEL_DIR/onnx/model.onnx $BUNDLE_DIR/onnx/
[ -f $MODEL_DIR/onnx/model.onnx.data ] && cp $MODEL_DIR/onnx/model.onnx.data $BUNDLE_DIR/onnx/
cp $MODEL_DIR/tokenizer.json $BUNDLE_DIR/
cp $MODEL_DIR/calibration.json $BUNDLE_DIR/
cp $MODEL_DIR/label_to_int.json $BUNDLE_DIR/

# Generate the manifest (sha256 each file, point url at the release tag)
TAG=v0.2.0   # match the Python release tag
.venv/bin/python - <<EOF
import hashlib, json
from pathlib import Path
base = Path("$BUNDLE_DIR")
release_url = "https://github.com/InferRoute/inferroute/releases/download/$TAG"
files = []
for rel in ["onnx/model.onnx", "tokenizer.json", "calibration.json", "label_to_int.json"]:
    p = base / rel
    if not p.exists():
        continue
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    files.append({"path": rel, "url": f"{release_url}/{p.name}", "sha256": sha})
# Optional sidecar (large model weights)
for sidecar in base.glob("onnx/model.onnx.data"):
    sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    files.append({"path": f"onnx/{sidecar.name}", "url": f"{release_url}/{sidecar.name}", "sha256": sha})
manifest = {"version": "$TAG", "files": files}
(base / "classifier-v0-manifest.json").write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest, indent=2))
EOF
```

## 3. Attach to the GitHub Release

Upload these as release assets to the matching tag (`v0.2.0`):

- `classifier-v0-manifest.json`   ← the daemon's entry point; must be named exactly this
- `model.onnx`                    ← rename so the URLs in the manifest match
- `model.onnx.data`               ← if your export uses external weights
- `tokenizer.json`
- `calibration.json`
- `label_to_int.json`

Easiest via `gh`:

```bash
gh release create $TAG \
  --title "$TAG" \
  --notes "$(git log --oneline $(git describe --tags --abbrev=0 HEAD^)..HEAD)" \
  $BUNDLE_DIR/classifier-v0-manifest.json \
  $BUNDLE_DIR/onnx/model.onnx \
  $BUNDLE_DIR/onnx/model.onnx.data \
  $BUNDLE_DIR/tokenizer.json \
  $BUNDLE_DIR/calibration.json \
  $BUNDLE_DIR/label_to_int.json
```

The daemon's default `classifier_bootstrap_url` uses `/releases/latest/download/`,
so once this release is published, every user running `ir add local-routing`
gets these files automatically.

## 4. Smoke-test the published release

```bash
# In a clean env that doesn't have the model on disk
pip install --upgrade inferroute
rm -rf ~/.inferroute/models/classifier-v0
ir add local-routing --no-service --no-shell-edit --yes
# Should print: ✓ Installed model version v0.2.0 at ~/.inferroute/models/classifier-v0
ls ~/.inferroute/models/classifier-v0/
```

## Common slip-ups

- **Filename mismatch**: the manifest's `url` field must match the actual
  filename you uploaded. `model.onnx.data` in the manifest doesn't help if
  you uploaded it as `weights.bin`.
- **Tag drift**: if you bump the Python version but forget to tag, the next
  `ir add local-routing` still pulls the OLD model from `/releases/latest/`.
  Always tag both halves at the same version.
- **Pre-release tag**: GitHub's `/releases/latest/` ignores pre-releases. If
  you ship a `v0.3.0-rc1` and don't mark it as a full release, users still
  see `v0.2.0`. Use this on purpose during testing, but remember to flip
  the "Set as the latest release" toggle for the real ship.
