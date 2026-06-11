"""Real-cost wiring: daemon captures server-reported `usage.cost` and keeps a
per-session running total the ir status line reads.

Cost path (see shared-docs/inferroute/goose-real-cost-display-spec.md):
  cc-proxy-prod emits usage.cost (USD) → daemon proxy parses it (float, not just
  ints) → recorder accumulates per session into <base>/sessions/<sid>.cost →
  ir status line reads that file. CC's own cost.total_cost_usd is NOT used (it
  mis-prices our routed models).
"""
import json

from inferroute_local.proxy import _merge_usage, _apply_obj, _extract_json
from inferroute_local.recorder import Recorder


# --- proxy: usage.cost (a float) survives parsing, ints still kept, bools dropped ---

def test_merge_usage_keeps_cost_float_and_int_tokens():
    dst = {}
    _merge_usage(dst, {
        "input_tokens": 3156, "output_tokens": 312,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "cost": 0.000287, "cost_currency": "USD",
        "some_bool": True,  # bool is an int subclass — must be skipped
    })
    assert dst["input_tokens"] == 3156
    assert dst["output_tokens"] == 312
    assert dst["cost"] == 0.000287
    assert dst["cost_currency"] == "USD"
    assert "some_bool" not in dst


def test_merge_usage_ignores_non_dict():
    dst = {"x": 1}
    _merge_usage(dst, None)
    _merge_usage(dst, "nope")
    assert dst == {"x": 1}


def test_streaming_message_delta_carries_cost():
    usage = {}
    # message_start: tokens, no cost yet
    _apply_obj({"type": "message_start", "message": {"model": "kimi",
               "usage": {"input_tokens": 100}}}, usage)
    # message_delta: final output_tokens + cost (where inferroute emits it)
    stop, _ = _apply_obj({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                          "usage": {"output_tokens": 50, "cost": 0.0012}}, usage)
    assert stop == "end_turn"
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["cost"] == 0.0012


def test_nonstreaming_extract_json_carries_cost():
    raw = json.dumps({"stop_reason": "end_turn", "model": "kimi",
                      "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.0009}}).encode()
    usage, stop, served = _extract_json(raw)
    assert stop == "end_turn" and served == "kimi"
    assert usage["cost"] == 0.0009


# --- recorder.note_cost: per-session .cost file, decoupled from record_level ---

def _rec(tmp_path, level="metadata"):
    return Recorder(tmp_path, level=level)


def test_note_cost_accumulates_across_turns(tmp_path):
    r = _rec(tmp_path)
    sid = "a" * 32
    r.note_cost(sid, 0.10)
    r.note_cost(sid, 0.05)
    cost_file = tmp_path / "sessions" / f"{sid}.cost"
    assert cost_file.is_file()
    assert abs(float(cost_file.read_text()) - 0.15) < 1e-9


def test_note_cost_works_with_recording_OFF(tmp_path):
    # THE point of decoupling (option B): cost is a content-free number, so it is
    # captured even when recording is fully off ("store nothing, still show price").
    r = _rec(tmp_path, level="off")
    assert not r.enabled  # corpus disabled...
    sid = "f" * 32
    r.note_cost(sid, 0.42)
    cost_file = tmp_path / "sessions" / f"{sid}.cost"
    assert cost_file.is_file()  # ...but the price still lands
    assert abs(float(cost_file.read_text()) - 0.42) < 1e-9
    # and no corpus was written
    assert not (tmp_path / "events").exists() or not list((tmp_path / "events").glob("*.jsonl"))


def test_note_cost_seeds_from_disk_on_fresh_recorder(tmp_path):
    sid = "b" * 32
    (tmp_path / "sessions").mkdir(parents=True)
    (tmp_path / "sessions" / f"{sid}.cost").write_text("1.000000")  # prior daemon run
    r = _rec(tmp_path)  # fresh process, empty in-memory total
    r.note_cost(sid, 0.25)
    assert abs(float((tmp_path / "sessions" / f"{sid}.cost").read_text()) - 1.25) < 1e-9


def test_note_cost_ignores_absent_zero_or_nonnumeric(tmp_path):
    r = _rec(tmp_path)
    r.note_cost("c" * 8, None)
    r.note_cost("d" * 8, 0.0)
    r.note_cost("g" * 8, "nope")
    r.note_cost("h" * 8, True)  # bool is an int subclass — must be ignored
    assert list((tmp_path / "sessions").glob("*.cost")) == []


def test_cost_usd_recorded_in_outcome_event(tmp_path):
    # The outcome event still logs the per-turn cost when recording is enabled.
    r = _rec(tmp_path)
    r.record_outcome(turn_id="t", session_id="e" * 8, status=200, ttft_ms=1, total_ms=1,
                     usage={"cost": 0.0033}, stop_reason="end_turn", served_model="kimi")
    r.flush()
    events = list((tmp_path / "events").glob("events-*.jsonl"))
    assert events
    rows = [json.loads(l) for l in events[0].read_text().splitlines()]
    outcome = [e for e in rows if e["kind"] == "outcome"][0]
    assert outcome["cost_usd"] == 0.0033


def test_note_cost_unsafe_session_id_does_not_escape_sessions_dir(tmp_path):
    r = _rec(tmp_path)
    r.note_cost("../../etc/pwned", 0.5)  # path-traversal id must be refused, never raise
    assert not (tmp_path.parent / "etc" / "pwned.cost").exists()
    assert list((tmp_path / "sessions").glob("*.cost")) == []


# --- full daemon path: streaming response → cost file, even with recording off ---

def test_proxy_records_cost_through_recording_stream_with_recording_off(tmp_path):
    import asyncio
    from inferroute_local.config import Config
    from inferroute_local.proxy import InferrouteProxy

    cfg = Config(record_dir=str(tmp_path), record_level="off")
    proxy = InferrouteProxy(cfg)
    sid = "z" * 32

    async def fake_stream():
        # a minimal Anthropic SSE stream carrying usage.cost on message_delta
        yield b'event: message_start\ndata: {"type":"message_start","message":{"model":"kimi","usage":{"input_tokens":9}}}\n\n'
        yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":16,"cost":0.0123,"cost_currency":"USD"}}\n\n'

    async def drive():
        agen = proxy._recording_stream(
            fake_stream(), turn_id=None, session_id=sid, streaming=True,
            chosen_model="kimi", status=200, start=0.0,
        )
        async for _ in agen:  # exhaust so the finally block runs
            pass
        await proxy.close()

    asyncio.run(drive())
    cost_file = tmp_path / "sessions" / f"{sid}.cost"
    assert cost_file.is_file()
    assert abs(float(cost_file.read_text()) - 0.0123) < 1e-9
