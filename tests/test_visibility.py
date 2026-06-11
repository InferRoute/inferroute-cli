"""Recording-visibility headers the daemon emits upstream (Tier 1 + content hash).

The daemon is the only component that sees plaintext on the user's machine, so it
emits two headers when forwarding (recording-visibility-spec.md):
  x-inferroute-recording: full|metadata|off   (disposition; config metadata)
  x-inferroute-content-hash: <sha256>          (only when a corpus is recorded)

The content hash MUST equal the hash the recorder stores locally — that identity
is what lets the server index line up with the local corpus and holds across the
TEE transition.
"""
import json

from inferroute_local.config import Config
from inferroute_local.proxy import InferrouteProxy
from inferroute_local.recorder import Recorder, new_user_block_hash


_BODY = {
    "model": "kimi",
    "messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": [{"type": "text", "text": "do the thing"}]},
    ],
}


def test_new_user_block_hash_matches_recorder_stored(tmp_path):
    # The hash emitted upstream must equal the one record_choice writes to disk.
    r = Recorder(tmp_path, level="metadata")
    r.record_choice(body=_BODY, headers={"x-inferroute-session": "s"})
    r.flush()
    events = list((tmp_path / "events").glob("events-*.jsonl"))
    choice = [json.loads(l) for l in events[0].read_text().splitlines() if '"choice"' in l][0]
    assert choice["new_user_block_hash"] == new_user_block_hash(_BODY)
    assert new_user_block_hash(_BODY)  # non-empty


def test_new_user_block_hash_none_without_user_message():
    assert new_user_block_hash({"messages": [{"role": "assistant", "content": "x"}]}) is None
    assert new_user_block_hash({}) is None


def _proxy(tmp_path, level):
    return InferrouteProxy(Config(record_dir=str(tmp_path), record_level=level))


def test_visibility_headers_full_and_metadata_emit_hash(tmp_path):
    for level in ("full", "metadata"):
        h = _proxy(tmp_path, level)._visibility_headers(_BODY)
        assert h["x-inferroute-recording"] == level
        assert h["x-inferroute-content-hash"] == new_user_block_hash(_BODY)


def test_visibility_headers_off_is_disposition_only_no_hash(tmp_path):
    # cost-only mode: the daemon is up (so disposition='off' is reported) but no
    # corpus → no content fingerprint leaves.
    h = _proxy(tmp_path, "off")._visibility_headers(_BODY)
    assert h["x-inferroute-recording"] == "off"
    assert "x-inferroute-content-hash" not in h


def test_visibility_headers_no_user_block_omits_hash(tmp_path):
    h = _proxy(tmp_path, "full")._visibility_headers({"messages": []})
    assert h["x-inferroute-recording"] == "full"
    assert "x-inferroute-content-hash" not in h


def test_forward_merges_visibility_headers_over_allowlist(tmp_path):
    # _forward must merge the daemon's headers in (the allow-list would otherwise
    # strip them). Verify via the same merge the method does.
    from inferroute_local.proxy import _forward_headers
    base = _forward_headers({"authorization": "Bearer k", "x-inferroute-session": "s"})
    extra = _proxy(tmp_path, "full")._visibility_headers(_BODY)
    base.update(extra)
    assert base["x-inferroute-recording"] == "full"
    assert base["x-inferroute-content-hash"] == new_user_block_hash(_BODY)
    assert base["authorization"] == "Bearer k"  # didn't clobber auth
