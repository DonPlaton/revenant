"""Regressions for a review pass over the whole tool.

Each of these failed before the fix it names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revenant  # noqa: E402
import revenant_agents as agents  # noqa: E402
import revenant_gui as gui  # noqa: E402
import revenant_terminals as terminals  # noqa: E402

CLAUDE = agents.AGENTS["claude-code"]


def _session(**kwargs) -> revenant.Session:
    base = dict(session_id="s", transcript=Path("s.jsonl"), project_slug="slug", cwd=Path("/tmp/s"))
    return revenant.Session(**{**base, **kwargs})


# --------------------------------------------------------------------------- #
# choosing
# --------------------------------------------------------------------------- #


def test_a_mistyped_range_does_not_allocate_a_billion_entries() -> None:
    """`1-999999999` used to build the whole range before bounds-checking it."""
    sessions = [_session(session_id=str(n)) for n in range(3)]
    picked = revenant.pick(sessions, reader=lambda _: "1-999999999")
    assert [s.session_id for s in picked] == ["0", "1", "2"]


def test_a_backwards_range_is_simply_empty() -> None:
    sessions = [_session(session_id=str(n)) for n in range(3)]
    assert revenant.pick(sessions, reader=lambda _: "3-1") == []


# --------------------------------------------------------------------------- #
# launching
# --------------------------------------------------------------------------- #


def test_one_running_session_does_not_hold_up_the_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The desktop app always opened what it safely could; the CLI refused the lot."""
    opened: list[int] = []

    def fake_run(plan):
        opened.append(len(plan.commands))
        return len(plan.commands), ""

    monkeypatch.setattr(terminals, "run", fake_run)
    sessions = [
        _session(session_id="dead-1", cwd=Path("/tmp/a")),
        _session(session_id="alive", cwd=Path("/tmp/b"), live_reason="process 42"),
        _session(session_id="dead-2", cwd=Path("/tmp/c")),
    ]
    assert revenant.launch(sessions) == 0
    out = capsys.readouterr().out
    assert "Holding back 1" in out
    assert opened, "the two dead sessions still had to be opened"


def test_everything_running_is_still_a_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    sessions = [_session(session_id="alive", live_reason="process 42")]
    assert revenant.launch(sessions) == 2
    assert "Holding back 1" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# emitted launchers
# --------------------------------------------------------------------------- #


def test_the_emitted_windows_launcher_honours_window_and_profile() -> None:
    script = revenant.render_launcher([_session()], shell="pwsh", window="0", profile="Ubuntu")
    assert "wt.exe -w '0'" in script
    assert "-p 'Ubuntu'" in script


def test_a_terminal_the_wt_form_cannot_express_gets_its_own_plan() -> None:
    """--terminal used to be dropped on the floor for .ps1 targets."""
    script = revenant.render_launcher(
        [_session()], shell="pwsh", terminal=terminals.WindowsConsole()
    )
    assert "wt.exe" not in script
    assert "Start-Process" in script
    assert "-WorkingDirectory" in script


def test_the_emitted_cmd_launcher_escapes_percent_signs() -> None:
    r"""In a batch file C:\100%done reads as a variable unless the % is doubled."""
    script = revenant.render_launcher([_session(cwd=Path(r"C:\100%done"))], shell="cmd")
    assert "100%%done" in script


def test_an_emitted_tmux_script_can_be_run_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    script = revenant.render_launcher([_session()], shell="bash", terminal=terminals.Tmux())
    assert "new-session -A -d" in script, "a second run must not fail on the name"
    assert "|| true" in script, "clearing up a window that is not there is not a failure"


# --------------------------------------------------------------------------- #
# reading transcripts
# --------------------------------------------------------------------------- #


def _transcript(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_head_stops_as_soon_as_it_has_what_it_came_for(tmp_path: Path) -> None:
    """It used to parse eighty records however early the answer appeared."""
    first = {
        "type": "user",
        "cwd": "/home/me/api",
        "version": "2.1.258",
        "gitBranch": "main",
        "timestamp": "2026-09-01T10:00:00.000Z",
    }
    noise = {"type": "assistant", "message": {"content": "x" * 200}}
    path = _transcript(tmp_path / "s.jsonl", [first] + [noise] * 300)

    read: list[int] = []
    original = agents._records

    def counting(*args, **kwargs):
        for index, record in enumerate(original(*args, **kwargs)):
            read.append(index)
            yield record

    import unittest.mock

    with unittest.mock.patch.object(agents, "_records", counting):
        meta = CLAUDE.head(path)
    assert meta["cwd"] == "/home/me/api"
    assert len(read) <= 4, f"parsed {len(read)} records to read the first one"


def test_reading_the_prompts_also_learns_the_name(tmp_path: Path) -> None:
    """Both answers live in one window, so it is read once, not twice."""
    agents._TITLE_CACHE.clear()
    path = _transcript(
        tmp_path / "s.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "fix the parser"}},
            {"type": "ai-title", "aiTitle": "Parser fixes", "sessionId": "s"},
        ],
    )
    CLAUDE.tail(path)

    reads: list[int] = []
    original = agents._tail_lines

    def counting(*args, **kwargs):
        reads.append(1)
        yield from original(*args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(agents, "_tail_lines", counting):
        assert CLAUDE.title(path) == "Parser fixes"
    assert reads == [], "the name was already known"


def test_a_changed_transcript_is_read_again(tmp_path: Path) -> None:
    agents._TITLE_CACHE.clear()
    path = _transcript(tmp_path / "s.jsonl", [{"type": "ai-title", "aiTitle": "First", "sessionId": "s"}])
    assert CLAUDE.title(path) == "First"
    _transcript(path, [{"type": "custom-title", "customTitle": "Renamed", "sessionId": "s"}])
    assert CLAUDE.title(path) == "Renamed"


def test_a_name_beyond_the_first_window_is_still_found(tmp_path: Path) -> None:
    """The cheap pass reads a few pages; the whole tail is the fallback."""
    agents._TITLE_CACHE.clear()
    filler = {"type": "assistant", "message": {"content": "x" * 900}}
    records = [{"type": "custom-title", "customTitle": "Buried", "sessionId": "s"}]
    records += [filler] * 120
    path = _transcript(tmp_path / "s.jsonl", records)
    assert path.stat().st_size > agents.TITLE_BYTES
    assert CLAUDE.title(path) == "Buried"


def test_naming_skips_sessions_that_already_know_their_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Path] = []
    monkeypatch.setattr(type(CLAUDE), "title", lambda self, path: asked.append(path) or "")
    monkeypatch.setattr(type(CLAUDE), "titles", lambda self, root: {})
    revenant.name_sessions(
        [
            _session(title="Already named", transcript=Path("/c/projects/slug/a.jsonl")),
            _session(session_id="b", transcript=Path("/c/projects/slug/b.jsonl")),
        ]
    )
    assert len(asked) == 1


# --------------------------------------------------------------------------- #
# the desktop backend
# --------------------------------------------------------------------------- #


def test_copied_commands_match_the_shell_the_viewer_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """PowerShell quoting doubles an apostrophe, which bash then swallows."""
    backend = gui.Backend()
    session = _session(cwd=Path("/home/u/dev/o'brien"))
    monkeypatch.setattr(backend, "_by_id", lambda ids, **kw: [session])
    monkeypatch.setattr(gui.os, "name", "posix")
    assert r"o'\''brien" in backend.commands(["s"], days=1)["text"]
    monkeypatch.setattr(gui.os, "name", "nt")
    assert "o''brien" in backend.commands(["s"], days=1)["text"]


def test_serving_the_page_counts_as_a_sign_of_life() -> None:
    """Only the first API call used to, so a slow browser could be shut down
    before it ever made one."""
    import urllib.request

    backend = gui.Backend()
    server, _ = gui.serve(backend)
    port = server.server_address[1]
    try:
        for path in ("/", "/api/ping"):
            backend.last_seen = 0.0
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}?t={backend.token}")
            request.add_header("Host", f"127.0.0.1:{port}")
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
            assert backend.last_seen > 0.0, f"{path} left the clock running down"
    finally:
        backend.request_stop()
        server.shutdown()


def test_the_idle_clock_starts_when_the_window_does() -> None:
    """It used to start at construction, so a slow browser could be killed first."""
    backend = gui.Backend()
    backend.last_seen = 0.0  # as if the process had been up for a very long time
    backend.request_stop()
    backend.wait_until_idle(grace=20.0)
    assert backend.last_seen > 0.0, "waiting resets the clock before it counts down"
