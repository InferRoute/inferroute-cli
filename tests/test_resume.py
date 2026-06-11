"""`ir --resume` resolution: argv parsing + the inferroute-aware session listing.

The menu (`resume._pick`) is a full-screen TUI and isn't unit-tested; the logic
that feeds it — parsing the resume flags, finding the cwd's transcripts, marking
YOUR inferroute sessions (from launch.launch_index) apart from native ones, and
annotating them with model/cost — is.
"""
import json
import os

from inferroute_cli import resume


# ── argv parsing ──────────────────────────────────────────────────────────────

def test_parse_modes_and_ids():
    assert resume._parse(["--resume"]) == ("menu", None, [])
    assert resume._parse(["-r"]) == ("menu", None, [])
    assert resume._parse(["--continue"]) == ("continue", None, [])
    assert resume._parse(["-c"]) == ("continue", None, [])
    assert resume._parse(["--resume", "abc123"]) == ("menu", "abc123", [])
    assert resume._parse(["--resume=abc123"]) == ("menu", "abc123", [])
    # a following flag is NOT consumed as an id; other passthrough survives
    assert resume._parse(["--resume", "--verbose"]) == ("menu", None, ["--verbose"])
    assert resume._parse(["--foo", "-c", "bar"]) == ("continue", None, ["--foo", "bar"])


# ── session listing + IR/native classification ────────────────────────────────

def _write_transcript(d, sid, *, title=None, branch="main", model=None, user="hello"):
    rows = []
    rows.append({"type": "user", "sessionId": sid, "gitBranch": branch,
                 "message": {"role": "user", "content": user}})
    if model:
        rows.append({"type": "assistant", "message": {"role": "assistant", "model": model}})
    if title:
        rows.append({"type": "assistant", "aiTitle": title, "message": {}})
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    cwd = "/work/proj"
    proj = tmp_path / ".claude" / "projects" / cwd.replace("/", "-")
    proj.mkdir(parents=True)
    return cwd, proj


def test_list_marks_inferroute_sessions_and_reads_cost(tmp_path, monkeypatch):
    cwd, proj = _setup(tmp_path, monkeypatch)
    # one ir session (in the index + a cost file) and one native session
    _write_transcript(proj, "ir1", title="Fix the bug", model="moonshotai/Kimi-K2.6-TEE")
    _write_transcript(proj, "native1", title="Plain claude chat", model="claude-opus-4-8")
    # ir index entry for ir1 only
    idx = tmp_path / ".config" / "inferroute"
    idx.mkdir(parents=True)
    (idx / "launches.jsonl").write_text(
        json.dumps({"id": "ir1", "model": "moonshotai/Kimi-K2.6-TEE", "lane": "economy",
                    "cwd": cwd, "ts": 1.0}) + "\n")
    # cost file for ir1
    sess = tmp_path / ".inferroute" / "sessions"
    sess.mkdir(parents=True)
    (sess / "ir1.cost").write_text("0.413700")

    sessions = resume.list_sessions(cwd)
    by_id = {s.id: s for s in sessions}
    assert set(by_id) == {"ir1", "native1"}

    ir1 = by_id["ir1"]
    assert ir1.is_ir is True
    assert ir1.model == "moonshotai/Kimi-K2.6-TEE"
    assert ir1.lane == "economy"
    assert abs(ir1.cost_usd - 0.4137) < 1e-9
    assert ir1.title == "Fix the bug"

    nat = by_id["native1"]
    assert nat.is_ir is False
    assert nat.cost_usd is None
    assert nat.lane is None

    # inferroute sessions are grouped ahead of native ones
    assert sessions[0].is_ir and not sessions[-1].is_ir


def test_list_falls_back_to_first_user_message_for_title(tmp_path, monkeypatch):
    cwd, proj = _setup(tmp_path, monkeypatch)
    _write_transcript(proj, "s1", title=None, user="please refactor the parser")
    s = resume.list_sessions(cwd)[0]
    assert s.title == "please refactor the parser"


def test_newest_returns_most_recent(tmp_path, monkeypatch):
    cwd, proj = _setup(tmp_path, monkeypatch)
    _write_transcript(proj, "old", title="old")
    _write_transcript(proj, "new", title="new")
    os.utime(proj / "old.jsonl", (1000, 1000))
    os.utime(proj / "new.jsonl", (2000, 2000))
    assert resume.newest(cwd) == "new"


def test_empty_or_missing_project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert resume.list_sessions("/nowhere/at/all") == []
    assert resume.newest("/nowhere/at/all") is None


# ── menu scrolls as the cursor moves (the bug: ListView was height:auto) ──────

def _mk(n):
    return [resume.Session(id=f"s{i}", title=f"Session {i}", mtime=float(1000 - i),
                           branch="main", is_ir=(i % 2 == 0),
                           model="moonshotai/Kimi-K2.6-TEE" if i % 2 == 0 else None,
                           lane="standard" if i % 2 == 0 else None,
                           cost_usd=0.12 if i % 2 == 0 else None)
            for i in range(n)]


def test_menu_scrolls_when_moving_past_the_fold():
    import asyncio

    async def drive():
        app = resume._build_resume_app(_mk(40))   # far more than fit on screen
        async with app.run_test(size=(100, 24)) as pilot:
            lv = app.query_one("#picker")
            assert lv.scroll_offset.y == 0          # starts at top
            await pilot.press(*(["down"] * 25))     # walk past the visible fold
            assert lv.index == 25                   # highlight advanced
            assert lv.scroll_offset.y > 0           # ...and the list scrolled to follow

    asyncio.run(drive())


def test_menu_enter_returns_chosen_session_id():
    import asyncio

    async def drive():
        app = resume._build_resume_app(_mk(40))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("down", "down", "enter")  # pick the 3rd row
            return app.chosen

    assert asyncio.run(drive()) == "s2"
