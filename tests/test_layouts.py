"""Where the revived sessions land: tabs of one window, or a window each.

Every plan is built without running anything, so the whole matrix - fourteen
backends against two layouts - is exercised on all three platforms.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revenant  # noqa: E402
import revenant_gui as gui  # noqa: E402
import revenant_terminals as terminals  # noqa: E402


JOBS = [
    terminals.Job("payments-api", "/home/me/payments api", "claude --resume abc"),
    terminals.Job("ml-pipeline", "/home/me/ml", "codex resume def"),
]


def _session(session_id: str = "abc", cwd: str = "/tmp/x") -> revenant.Session:
    return revenant.Session(
        session_id=session_id, transcript=Path("x.jsonl"), project_slug="s", cwd=Path(cwd)
    )


# --------------------------------------------------------------------------- #
# the contract every backend keeps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", terminals.ALL, ids=[c.key for c in terminals.ALL])
@pytest.mark.parametrize("layout", terminals.LAYOUTS)
def test_every_backend_answers_for_every_layout(cls: type[terminals.Terminal], layout: str) -> None:
    """A backend either honours the layout or says which one it used instead."""
    backend = cls()
    plan = backend.plan(JOBS, layout=layout)

    assert plan.layout in terminals.LAYOUTS
    assert plan.commands, "a plan that opens nothing is not an answer"
    if layout in backend.layouts:
        assert plan.layout == layout
        assert backend.demoted(layout) == ""
    else:
        assert plan.layout == backend.layouts[0]
        assert backend.label in plan.note and layout in plan.note


@pytest.mark.parametrize("cls", terminals.ALL, ids=[c.key for c in terminals.ALL])
def test_a_backend_declares_at_least_one_layout(cls: type[terminals.Terminal]) -> None:
    backend = cls()
    assert backend.layouts, f"{backend.key} offers nothing at all"
    assert set(backend.layouts) <= set(terminals.LAYOUTS)
    # `tabs` is the old name for the same fact and the table still prints it.
    assert backend.tabs == (terminals.LAYOUT_TABS in backend.layouts)


def test_no_layout_named_means_tabs() -> None:
    assert terminals.WindowsTerminal().plan(JOBS).layout == terminals.LAYOUT_TABS
    assert terminals.GnomeTerminal().plan(JOBS).layout == terminals.LAYOUT_TABS


# --------------------------------------------------------------------------- #
# per backend
# --------------------------------------------------------------------------- #


def _is_wt(argv: list[str], window: str) -> bool:
    """argv[0] is a resolved path, not the literal alias, so compare the name."""
    return Path(argv[0]).name.lower() == "wt.exe" and argv[1:3] == ["-w", window]


def test_windows_terminal_opens_a_window_per_session() -> None:
    plan = terminals.WindowsTerminal().plan(JOBS, layout="windows")
    assert len(plan.commands) == len(JOBS), "each session needs a call of its own"
    for argv in plan.commands:
        assert _is_wt(argv, "new"), "-w new is what makes it a new window"
        assert argv.count("new-tab") == 1
        assert ";" not in argv


def test_a_named_target_window_does_not_defeat_the_windows_layout() -> None:
    """`--window 0` would fold them back into tabs, which is what was declined."""
    plan = terminals.WindowsTerminal().plan(JOBS, layout="windows", window="0")
    assert all(_is_wt(argv, "new") for argv in plan.commands)


def test_windows_terminal_still_honours_the_profile_per_window() -> None:
    plan = terminals.WindowsTerminal().plan(JOBS, layout="windows", profile="Ubuntu")
    assert all(argv.count("-p") == 1 and "Ubuntu" in argv for argv in plan.commands)


def test_iterm2_makes_a_window_each_and_no_tabs() -> None:
    script = terminals.ITerm2().plan(JOBS, layout="windows").commands[0][2]
    assert script.count("create window with default profile") == len(JOBS)
    assert "create tab" not in script


def test_iterm2_tabs_are_untouched() -> None:
    script = terminals.ITerm2().plan(JOBS, layout="tabs").commands[0][2]
    assert script.count("create window with default profile") == 1
    assert script.count("create tab with default profile") == len(JOBS) - 1


def test_gnome_terminal_swaps_tab_for_window() -> None:
    tabs = terminals.GnomeTerminal().plan(JOBS, layout="tabs")
    windows = terminals.GnomeTerminal().plan(JOBS, layout="windows")
    assert all("--tab" in argv and "--window" not in argv for argv in tabs.commands)
    assert all("--window" in argv and "--tab" not in argv for argv in windows.commands)
    # The payload is the only thing that must not change with the layout.
    assert [argv[-1] for argv in tabs.commands] == [argv[-1] for argv in windows.commands]


def test_konsole_drops_new_tab_for_windows() -> None:
    windows = terminals.Konsole().plan(JOBS, layout="windows")
    assert all("--new-tab" not in argv for argv in windows.commands)
    assert all("--workdir" in argv for argv in windows.commands)


def test_xfce_terminal_splits_one_call_into_several() -> None:
    tabs = terminals.XfceTerminal().plan(JOBS, layout="tabs")
    windows = terminals.XfceTerminal().plan(JOBS, layout="windows")
    assert len(tabs.commands) == 1 and tabs.commands[0].count("--tab") == len(JOBS)
    assert len(windows.commands) == len(JOBS)
    assert all("--tab" not in argv for argv in windows.commands)


def test_a_windows_only_backend_says_so_rather_than_pretending() -> None:
    plan = terminals.Kitty().plan(JOBS, layout="tabs")
    assert plan.layout == terminals.LAYOUT_WINDOWS
    assert plan.note == "kitty cannot open tabs; opened one window per session."


def test_tmux_keeps_its_attach_line_when_it_cannot_do_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    plan = terminals.Tmux().plan(JOBS, layout="windows")
    assert plan.layout == terminals.LAYOUT_TABS
    assert "cannot open windows" in plan.note
    assert "tmux attach -t revenant" in plan.note, "the note that matters must survive"


# --------------------------------------------------------------------------- #
# choosing a backend for a layout
# --------------------------------------------------------------------------- #


def _only(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    """Pretend exactly these backends are installed, on this platform."""
    wanted = set(keys)
    for cls in terminals.ALL:
        monkeypatch.setattr(cls, "available", lambda self, w=wanted: self.key in w)


def test_a_backend_that_can_do_the_layout_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(terminals, "here", lambda: "linux")
    # Ordered kitty-first on purpose: kitty cannot tab, so tabs must skip past it.
    monkeypatch.setitem(terminals.ORDER, "linux", (terminals.Kitty, terminals.Konsole))
    _only(monkeypatch, "kitty", "konsole")

    assert terminals.choose(layout="tabs").key == "konsole"
    assert terminals.choose(layout="windows").key == "kitty", "first available still wins"


def test_the_layout_is_a_preference_not_a_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing installed can do tabs, so something still has to open the sessions."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(terminals, "here", lambda: "linux")
    monkeypatch.setitem(terminals.ORDER, "linux", (terminals.Kitty, terminals.Xterm))
    _only(monkeypatch, "kitty", "xterm")
    assert terminals.choose(layout="tabs").key == "kitty"


def test_inside_tmux_the_layout_does_not_move_the_windows_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over SSH, honouring `windows` would open them on the far machine."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(terminals.Tmux, "available", lambda self: True)
    assert terminals.choose(layout="windows").key == "tmux"


def test_a_named_terminal_is_still_obeyed() -> None:
    assert terminals.choose("kitty", layout="tabs").key == "kitty"


def test_fallbacks_prefer_the_layout_without_dropping_anyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminals, "here", lambda: "linux")
    monkeypatch.setitem(
        terminals.ORDER, "linux", (terminals.Kitty, terminals.Xterm, terminals.Konsole)
    )
    _only(monkeypatch, "kitty", "xterm", "konsole", "tmux")
    order = [t.key for t in terminals.fallbacks(terminals.Foot(), layout="tabs")]
    assert order[0] == "konsole", "a tab-capable backend goes first"
    assert order[1:3] == ["kitty", "xterm"], "the usual order survives inside the group"
    assert set(order) == {"kitty", "xterm", "konsole", "tmux"}, "nobody is dropped"


def test_tmux_stays_the_last_resort_whatever_the_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a desktop, a detached session nobody attaches to looks like nothing happened."""
    monkeypatch.setattr(terminals, "here", lambda: "nt")
    monkeypatch.setitem(terminals.ORDER, "nt", (terminals.WindowsTerminal, terminals.WindowsConsole))
    _only(monkeypatch, "conhost", "tmux")
    order = [t.key for t in terminals.fallbacks(terminals.WindowsTerminal(), layout="tabs")]
    assert order == ["conhost", "tmux"], "tmux can tab, and still does not jump the queue"


# --------------------------------------------------------------------------- #
# what the caller is told
# --------------------------------------------------------------------------- #


def _report(monkeypatch: pytest.MonkeyPatch, backend: terminals.Terminal, layout: str) -> str:
    monkeypatch.setattr(terminals, "choose", lambda preferred=None, **_: backend)
    monkeypatch.setattr(terminals, "run", lambda plan: (len(plan.commands), plan.note))
    sink = io.StringIO()
    assert revenant.launch([_session(), _session("def", "/tmp/y")], layout=layout, stream=sink) == 0
    return sink.getvalue()


def test_launch_counts_tabs_as_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _report(monkeypatch, terminals.WindowsTerminal(), "tabs")
    assert "Opened 2 tabs in Windows Terminal." in out


def test_launch_counts_windows_as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _report(monkeypatch, terminals.WindowsTerminal(), "windows")
    assert "Opened 2 windows in Windows Terminal." in out


def test_launch_reports_what_happened_not_what_was_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _report(monkeypatch, terminals.Kitty(), "tabs")
    assert "Opened 2 windows in kitty." in out and "Opened 2 tabs" not in out
    assert "kitty cannot open tabs" in out


# --------------------------------------------------------------------------- #
# the command line
# --------------------------------------------------------------------------- #


def _root(tmp_path: Path) -> Path:
    """A config root holding one readable transcript."""
    root = tmp_path / ".claude"
    (root / "projects" / "slug").mkdir(parents=True)
    (root / "projects" / "slug" / "abc.jsonl").write_text(
        json.dumps(
            {"type": "user", "cwd": str(tmp_path), "message": {"role": "user", "content": "hi"}}
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _launch_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> dict:
    seen: dict = {}
    monkeypatch.setattr(revenant, "launch", lambda sessions, **kw: seen.update(kw) or 0)
    assert revenant.main(["--root", str(_root(tmp_path)), "--since", "7d", "--launch", *argv]) == 0
    return seen


def test_the_command_line_defaults_to_tabs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _launch_args(monkeypatch, tmp_path)["layout"] == "tabs"


def test_the_command_line_carries_the_windows_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _launch_args(monkeypatch, tmp_path, "--layout", "windows")
    assert seen["layout"] == "windows"
    assert seen["terminal"] is None, "a layout is not a way of pinning a backend"


def test_dry_run_shows_a_window_per_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(terminals, "choose", lambda preferred=None, **_: terminals.Konsole())
    sink = io.StringIO()
    revenant.launch(
        [_session("abc", "/tmp/a"), _session("def", "/tmp/b")],
        layout="windows",
        dry_run=True,
        stream=sink,
    )
    lines = [line for line in sink.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert all("--new-tab" not in line for line in lines)


def test_the_retired_no_tabs_flag_now_means_the_windows_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _launch_args(monkeypatch, tmp_path, "--no-tabs")
    assert seen["layout"] == "windows"
    assert seen["terminal"] is None, "the layout is the choice now, not a pinned backend"


def test_an_unknown_layout_is_refused_at_the_parser() -> None:
    with pytest.raises(SystemExit):
        revenant.build_parser().parse_args(["--layout", "panes"])


# --------------------------------------------------------------------------- #
# emitted scripts
# --------------------------------------------------------------------------- #


def test_the_powershell_launcher_can_open_windows_instead_of_tabs() -> None:
    sessions = [_session("abc", r"D:\Coding\alpha"), _session("def", r"D:\Coding\beta")]
    script = revenant.render_launcher(sessions, shell="pwsh", layout="windows")
    assert "wt.exe" not in script, "a window each needs no tabbed invocation"
    assert script.count("Start-Process $shell") == len(sessions)
    assert "$shell = if (Get-Command pwsh" in script, "the shell has to be defined before use"
    for session in sessions:
        assert str(session.cwd) in script


def test_the_powershell_launcher_still_defaults_to_tabs() -> None:
    script = revenant.render_launcher([_session("abc", r"D:\Coding\alpha")], shell="pwsh")
    assert "& $wt -w 'new'" in script


def test_the_bash_launcher_carries_the_layout() -> None:
    sessions = [_session("abc", "/home/me/alpha"), _session("def", "/home/me/beta")]
    script = revenant.render_launcher(
        sessions, shell="bash", terminal=terminals.GnomeTerminal(), layout="windows"
    )
    assert script.count("--window") == len(sessions)
    assert "--tab" not in script


def test_an_emitted_windows_launcher_names_no_session_twice(tmp_path: Path) -> None:
    """A copy-paste slip here would resume one session in two shells at once."""
    sessions = [_session("abc", r"D:\Coding\alpha"), _session("def", r"D:\Coding\beta")]
    script = revenant.render_launcher(sessions, shell="pwsh", layout="windows")
    for session in sessions:
        assert script.count(session.resume_command) == 1


# --------------------------------------------------------------------------- #
# the desktop app
# --------------------------------------------------------------------------- #


def test_the_app_passes_the_layout_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}
    monkeypatch.setattr(revenant, "launch", lambda sessions, **kw: seen.update(kw) or 0)
    backend = gui.Backend(root=str(tmp_path))
    monkeypatch.setattr(backend, "_by_id", lambda ids, **kw: [_session()])

    backend.revive(["abc"], days=7, layout="windows")
    assert seen["layout"] == "windows"


@pytest.mark.parametrize("supplied", ["", "panes", "TABS", "windows; rm -rf /"])
def test_the_app_falls_back_rather_than_refusing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, supplied: str
) -> None:
    """A layout the backend does not know is a version skew, not a reason to stall."""
    seen: dict = {}
    monkeypatch.setattr(revenant, "launch", lambda sessions, **kw: seen.update(kw) or 0)
    backend = gui.Backend(root=str(tmp_path))
    monkeypatch.setattr(backend, "_by_id", lambda ids, **kw: [_session()])

    backend.revive(["abc"], days=7, layout=supplied)
    assert seen["layout"] == terminals.DEFAULT_LAYOUT


def test_the_ui_offers_both_layouts_and_sends_the_choice() -> None:
    page = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
    for layout in terminals.LAYOUTS:
        assert f'data-layout="{layout}"' in page
    assert "days: stop().days, agent, layout" in page, "the choice has to reach /api/revive"
