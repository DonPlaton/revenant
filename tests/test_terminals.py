"""Terminal backends. Every plan is built without running anything, so all of
these run on all three platforms."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import terminals  # noqa: E402


JOBS = [
    terminals.Job("payments-api", "/home/me/payments api", "claude --resume abc"),
    terminals.Job("ml-pipeline", "/home/me/ml", "codex resume def"),
]
WINDOWS_JOBS = [
    terminals.Job("alpha", r"D:\Coding\alpha", "claude --resume abc"),
    terminals.Job("beta", r"D:\Coding\it's here", "claude --resume def"),
]


# --------------------------------------------------------------------------- #
# the shell payload
# --------------------------------------------------------------------------- #


def test_payload_quotes_a_directory_with_spaces() -> None:
    payload = terminals._posix_payload(JOBS[0])
    assert "cd '/home/me/payments api'" in payload
    assert "claude --resume abc" in payload


def test_payload_quotes_an_apostrophe() -> None:
    job = terminals.Job("x", "/home/me/it's here", "claude --resume abc")
    payload = terminals._posix_payload(job)
    # The quoting has to survive a round trip through the shell lexer.
    assert shlex.split(f"sh -c {shlex.quote(payload)}")[2] == payload
    assert "it's here" in shlex.split(payload.split("&&")[0])[1]


def test_payload_leaves_a_shell_behind() -> None:
    """Mirrors PowerShell's -NoExit: the window stays after the agent quits."""
    assert terminals._posix_payload(JOBS[0]).rstrip().startswith("cd ")
    assert "; exec " in terminals._posix_payload(JOBS[0])


# --------------------------------------------------------------------------- #
# per-backend argv
# --------------------------------------------------------------------------- #


def test_windows_terminal_makes_one_window_of_tabs() -> None:
    plan = terminals.WindowsTerminal().plan(WINDOWS_JOBS)
    assert len(plan.commands) == 1
    argv = plan.commands[0]
    assert argv[:3] == ["wt.exe", "-w", "new"]
    assert argv.count("new-tab") == 2
    assert argv.count(";") == 1
    assert r"D:\Coding\it's here" in argv


def test_windows_terminal_honours_window_and_profile() -> None:
    argv = terminals.WindowsTerminal().plan(WINDOWS_JOBS, window="0", profile="Ubuntu").commands[0]
    assert argv[:3] == ["wt.exe", "-w", "0"]
    assert argv.count("-p") == 2


def test_windows_console_sets_the_directory_without_cd() -> None:
    plan = terminals.WindowsConsole().plan(WINDOWS_JOBS)
    assert len(plan.commands) == 2
    argv = plan.commands[0]
    assert argv[:5] == ["cmd", "/c", "start", "", "/D"]
    assert argv[5] == r"D:\Coding\alpha"
    assert "-NoExit" in argv


def test_iterm2_opens_one_window_with_tabs() -> None:
    plan = terminals.ITerm2().plan(JOBS)
    assert len(plan.commands) == 1
    script = plan.commands[0][2]
    assert plan.commands[0][:2] == ["osascript", "-e"]
    assert script.count("create tab with default profile") == 1, "first job uses the window itself"
    assert script.count("write text") == 2
    assert "claude --resume abc" in script


def test_terminal_app_writes_one_script_per_session() -> None:
    script = terminals.MacTerminal().plan(JOBS).commands[0][2]
    assert script.count("do script") == 2


def test_applescript_escapes_quotes_and_backslashes() -> None:
    job = terminals.Job("x", '/home/me/say "hi"', "claude --resume abc")
    script = terminals.MacTerminal().plan([job]).commands[0][2]
    assert '\\"' in script
    assert script.count('do script "') == 1


def test_gnome_terminal_builds_one_call_with_tabs() -> None:
    plan = terminals.GnomeTerminal().plan(JOBS)
    assert len(plan.commands) == 1
    argv = plan.commands[0]
    assert argv[0] == "gnome-terminal"
    assert argv.count("--tab") == 2
    assert "--working-directory=/home/me/ml" in argv


def test_konsole_opens_a_tab_per_session() -> None:
    plan = terminals.Konsole().plan(JOBS)
    assert len(plan.commands) == 2
    assert plan.commands[0][:4] == ["konsole", "--new-tab", "--workdir", "/home/me/payments api"]


def test_xfce_terminal_passes_the_command_as_one_flag() -> None:
    argv = terminals.XfceTerminal().plan(JOBS).commands[0]
    assert sum(1 for part in argv if part.startswith("--command=")) == 2


@pytest.mark.parametrize(
    "backend,binary",
    [
        (terminals.Kitty, "kitty"),
        (terminals.WezTerm, "wezterm"),
        (terminals.Alacritty, "alacritty"),
        (terminals.Foot, "foot"),
    ],
)
def test_simple_unix_terminals_carry_directory_and_command(backend, binary: str) -> None:
    plan = backend().plan(JOBS)
    assert len(plan.commands) == 2
    argv = plan.commands[0]
    assert argv[0] == binary
    assert "/home/me/payments api" in argv
    assert argv[-3:-1] == ["sh", "-c"]
    assert "claude --resume abc" in argv[-1]


def test_ghostty_uses_its_own_flag_style() -> None:
    argv = terminals.Ghostty().plan(JOBS).commands[0]
    assert argv[0] == "ghostty"
    assert argv[1] == "--working-directory=/home/me/payments api"
    assert argv[2].startswith("-e=")


def test_xterm_titles_each_window() -> None:
    argv = terminals.Xterm().plan(JOBS).commands[0]
    assert argv[:3] == ["xterm", "-T", "payments-api"]


# --------------------------------------------------------------------------- #
# tmux
# --------------------------------------------------------------------------- #


def test_tmux_creates_a_session_then_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    plan = terminals.Tmux().plan(JOBS)
    assert plan.commands[0][:4] == ["tmux", "new-session", "-d", "-s"]
    assert plan.commands[1][1] == "new-window"
    assert "-t" in plan.commands[1]
    assert "tmux attach -t revenant" in plan.note


def test_tmux_inside_tmux_adds_windows_to_the_current_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    plan = terminals.Tmux().plan(JOBS)
    assert [argv[1] for argv in plan.commands] == ["new-window", "new-window"]
    assert "-t" not in plan.commands[0], "already attached, so no target"
    assert plan.note == ""


def test_tmux_names_windows_after_the_session() -> None:
    plan = terminals.Tmux().plan(JOBS)
    assert "payments-api" in plan.commands[0]
    assert "ml-pipeline" in plan.commands[1]


# --------------------------------------------------------------------------- #
# choosing
# --------------------------------------------------------------------------- #


def test_an_explicit_terminal_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert terminals.choose("konsole").key == "konsole"
    assert terminals.choose("iterm2").key == "iterm2"


def test_an_unknown_terminal_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        terminals.choose("nope")
    assert "Unknown terminal" in str(caught.value)


def test_being_inside_tmux_wins_when_nothing_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setattr(terminals.Tmux, "available", lambda self: True)
    assert terminals.choose().key == "tmux"


def test_choose_always_returns_something(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(terminals.shutil, "which", lambda name: None)
    for candidate in terminals.ALL:
        monkeypatch.setattr(candidate, "available", lambda self: False)
    assert terminals.choose() is not None


def test_every_backend_declares_its_platforms() -> None:
    for cls in terminals.ALL:
        assert cls.platforms, f"{cls.__name__} has no platforms"
        assert set(cls.platforms) <= {"nt", "darwin", "linux"}


def test_every_backend_key_is_unique() -> None:
    keys = [cls.key for cls in terminals.ALL]
    assert len(keys) == len(set(keys))


def test_plans_render_as_runnable_lines() -> None:
    plan = terminals.Konsole().plan(JOBS)
    rendered = plan.render().splitlines()
    assert len(rendered) == 2
    assert shlex.split(rendered[0])[0] == "konsole"


def test_supported_matches_the_current_platform() -> None:
    current = terminals.here()
    for cls in terminals.ALL:
        assert cls().supported() == (current in cls.platforms)


# --------------------------------------------------------------------------- #
# falling back
# --------------------------------------------------------------------------- #


def test_fallbacks_exclude_the_one_that_just_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminals.Terminal, "available", lambda self: self.supported())
    monkeypatch.setattr(terminals.Tmux, "available", lambda self: True)
    first = terminals.choose()
    others = terminals.fallbacks(first)
    assert first.key not in [t.key for t in others]
    assert others, "there is always somewhere else to try"


def test_launch_moves_on_when_a_terminal_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows Terminal can be present, look available, and still fail on run."""
    import revenant

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    class Broken(terminals.Terminal):
        key, label, platforms = "broken", "Broken", ("nt", "darwin", "linux")

        def available(self) -> bool:
            return True

        def plan(self, jobs):
            return terminals.Plan(self.key, [["definitely-not-a-program"]])

    class Works(terminals.Terminal):
        key, label, platforms = "works", "Works", ("nt", "darwin", "linux")
        opened: list = []

        def available(self) -> bool:
            return True

        def plan(self, jobs):
            return terminals.Plan(self.key, [["echo", job.label] for job in jobs])

    broken, works = Broken(), Works()
    monkeypatch.setattr(terminals, "choose", lambda preferred=None: broken)
    monkeypatch.setattr(terminals, "fallbacks", lambda after: [works])
    monkeypatch.setattr(terminals, "run", lambda plan: (0, "denied") if plan.terminal == "broken" else (1, ""))

    session = revenant.Session(
        session_id="abc", transcript=Path("x.jsonl"), project_slug="s", cwd=Path("/tmp/x")
    )
    assert revenant.launch([session]) == 0
    out = capsys.readouterr().out
    assert "trying Works" in out
    assert "Opened" in out


def test_a_pinned_terminal_is_not_second_guessed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import revenant

    class Broken(terminals.Terminal):
        key, label, platforms = "broken", "Broken", ("nt", "darwin", "linux")

        def available(self) -> bool:
            return True

        def plan(self, jobs):
            return terminals.Plan(self.key, [["nope"]])

    monkeypatch.setattr(terminals, "choose", lambda preferred=None: Broken())
    monkeypatch.setattr(terminals, "run", lambda plan: (0, "denied"))
    called = []
    monkeypatch.setattr(terminals, "fallbacks", lambda after: called.append(after) or [])

    session = revenant.Session(
        session_id="abc", transcript=Path("x.jsonl"), project_slug="s", cwd=Path("/tmp/x")
    )
    assert revenant.launch([session], terminal="broken") == 3
    assert not called, "the user asked for this terminal, so do not silently switch"
