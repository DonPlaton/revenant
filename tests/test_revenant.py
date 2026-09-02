"""Tests for Revenant. Everything runs against a synthetic config root - no real session is touched."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents  # noqa: E402
import revenant  # noqa: E402
import terminals  # noqa: E402


NOW = datetime.now(timezone.utc)


def _transcript(root: Path, slug: str, session_id: str, cwd: str, *, age_hours: float = 1.0) -> Path:
    directory = root / "projects" / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    started = (NOW - timedelta(hours=age_hours + 1)).isoformat().replace("+00:00", "Z")
    records = [
        {"type": "mode", "mode": "normal", "sessionId": session_id},
        {
            "type": "user",
            "cwd": cwd,
            "version": "2.1.257",
            "gitBranch": "main",
            "timestamp": started,
            "sessionId": session_id,
            "isMeta": True,
            "message": {"role": "user", "content": "<local-command-caveat>ignore me</local-command-caveat>"},
        },
        {
            "type": "user",
            "cwd": cwd,
            "timestamp": started,
            "sessionId": session_id,
            "message": {"role": "user", "content": "real question about the code"},
        },
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    stamp = (NOW - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _history(root: Path, rows: list[tuple[str, str, str, float]]) -> None:
    """rows: (sessionId, prompt, project, age_hours)"""
    lines = []
    for session_id, prompt, project, age_hours in rows:
        lines.append(
            json.dumps(
                {
                    "display": prompt,
                    "pastedContents": {},
                    "timestamp": str(int((NOW - timedelta(hours=age_hours)).timestamp() * 1000)),
                    "project": project,
                    "sessionId": session_id,
                },
                ensure_ascii=False,
            )
        )
    (root / "history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / ".claude"
    (base / "projects").mkdir(parents=True)
    (base / "sessions").mkdir(parents=True)
    _transcript(base, "D--Coding-alpha", "11111111-1111-1111-1111-111111111111", r"D:\Coding\alpha", age_hours=2)
    _transcript(base, "D--Coding-beta", "22222222-2222-2222-2222-222222222222", r"D:\Coding\beta", age_hours=50)
    _transcript(base, "D--Coding-alpha", "33333333-3333-3333-3333-333333333333", r"D:\Coding\alpha", age_hours=6)
    _history(
        base,
        [
            ("11111111-1111-1111-1111-111111111111", "/model", r"D:\Coding\alpha", 2.2),
            ("11111111-1111-1111-1111-111111111111", "почини тесты", r"D:\Coding\alpha", 2.0),
            ("22222222-2222-2222-2222-222222222222", "old work", r"D:\Coding\beta", 50.0),
            ("33333333-3333-3333-3333-333333333333", "add the parser", r"D:\Coding\alpha", 6.0),
        ],
    )
    # Revenant's own state must never land in the real user profile during tests.
    monkeypatch.setattr(revenant, "state_dir", lambda: tmp_path / "state")
    return base


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #


def test_default_agent_is_claude_code() -> None:
    assert revenant.get_agent(None) is revenant.CLAUDE_CODE
    assert revenant.get_agent("claude-code") is revenant.CLAUDE_CODE


def test_unknown_agent_is_rejected() -> None:
    with pytest.raises(SystemExit):
        revenant.get_agent("nonexistent-agent")


def test_agent_builds_its_own_resume_command() -> None:
    assert revenant.CLAUDE_CODE.resume_command("abc") == "claude --resume abc"


def test_agent_config_dir_prefers_explicit_then_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "from-env"))
    assert revenant.CLAUDE_CODE.config_dir() == tmp_path / "from-env"
    assert revenant.CLAUDE_CODE.config_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_state_dir_is_outside_the_agent_config() -> None:
    assert ".claude" not in revenant.state_dir().parts


# --------------------------------------------------------------------------- #
# time parsing
# --------------------------------------------------------------------------- #


def parse_delta(text: str, now: datetime) -> int:
    return int((now - revenant.parse_when(text, now=now)).total_seconds())


@pytest.mark.parametrize(
    "text,seconds",
    [("30s", 30), ("90m", 5400), ("24h", 86400), ("7d", 604800), ("2w", 1209600)],
)
def test_parse_when_durations(text: str, seconds: int) -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_delta(text, now) == seconds


def test_parse_when_absolute_date() -> None:
    parsed = revenant.parse_when("2026-09-01")
    assert parsed.tzinfo is not None
    assert parsed.astimezone().strftime("%Y-%m-%d") == "2026-09-01"


def test_parse_when_all_reaches_epoch() -> None:
    assert revenant.parse_when("all").year == 1970


def test_parse_when_rejects_garbage() -> None:
    with pytest.raises(Exception):
        revenant.parse_when("not-a-time")


def test_humanize_age() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert revenant.humanize_age(now - timedelta(seconds=5), now=now) == "5s"
    assert revenant.humanize_age(now - timedelta(minutes=5), now=now) == "5m"
    assert revenant.humanize_age(now - timedelta(hours=5), now=now) == "5h"
    assert revenant.humanize_age(now - timedelta(days=3), now=now) == "3d"
    assert revenant.humanize_age(None, now=now) == "?"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_scan_respects_window(root: Path) -> None:
    recent = revenant.scan_sessions(root, since=revenant.parse_when("24h"))
    assert {s.session_id[:8] for s in recent} == {"11111111", "33333333"}

    everything = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    assert len(everything) == 3


def test_scan_respects_until(root: Path) -> None:
    windowed = revenant.scan_sessions(
        root, since=revenant.parse_when("7d"), until=revenant.parse_when("24h")
    )
    assert {s.session_id[:8] for s in windowed} == {"22222222"}


def test_scan_extracts_metadata(root: Path) -> None:
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(root, since=revenant.parse_when("7d"))}
    alpha = sessions["11111111"]
    assert alpha.cwd == Path(r"D:\Coding\alpha")
    assert alpha.git_branch == "main"
    assert alpha.version == "2.1.257"
    assert alpha.turns == 1, "slash commands must not count as user turns"
    assert alpha.last_prompt == "почини тесты"
    assert alpha.label == "alpha"
    assert alpha.agent is revenant.CLAUDE_CODE
    assert alpha.resume_command.endswith(alpha.session_id)


@pytest.mark.parametrize(
    "cwd,expected",
    [
        (r"D:\Coding\alpha", "alpha"),
        ("/home/me/projects/beta", "beta"),
        ("C:/Users/me/src/gamma/", "gamma"),
        ("relative", "relative"),
    ],
)
def test_label_reads_the_last_segment_on_any_platform(cwd: str, expected: str) -> None:
    """A Windows-written config is readable on Linux, where a backslash is not a separator."""
    session = revenant.Session(
        session_id="x", transcript=Path("x.jsonl"), project_slug="s", cwd=Path(cwd)
    )
    assert session.label == expected


def test_live_name_wins_over_the_directory() -> None:
    session = revenant.Session(
        session_id="x",
        transcript=Path("x.jsonl"),
        project_slug="s",
        cwd=Path("/home/me/beta"),
        live_pid=1,
        live_name="beta-7c",
    )
    assert session.label == "beta-7c"


def test_scan_sorts_newest_first(root: Path) -> None:
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    ages = [s.last_active for s in sessions]
    assert ages == sorted(ages, reverse=True)


def test_scan_falls_back_to_transcript_when_history_missing(root: Path) -> None:
    (root / "history.jsonl").unlink()
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(root, since=revenant.parse_when("7d"))}
    alpha = sessions["11111111"]
    assert alpha.last_prompt == "real question about the code"
    assert alpha.turns == 1


def test_scan_survives_corrupt_transcript(root: Path) -> None:
    broken = root / "projects" / "D--Coding-alpha" / "44444444-4444-4444-4444-444444444444.jsonl"
    broken.write_text("{not json at all\n\x00\n", encoding="utf-8")
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    assert len(sessions) == 4  # present, just without metadata


def test_scan_returns_nothing_without_projects_dir(tmp_path: Path) -> None:
    assert revenant.scan_sessions(tmp_path, since=revenant.parse_when("all")) == []


def test_slug_filter(root: Path) -> None:
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"), slug_filter="beta")
    assert len(sessions) == 1


# --------------------------------------------------------------------------- #
# live detection - the safety-critical part
# --------------------------------------------------------------------------- #


def _register_live(root: Path, session_id: str, pid: int, cwd: str, name: str) -> None:
    (root / "sessions" / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": cwd,
                "name": name,
                "status": "busy",
                "startedAt": int(NOW.timestamp() * 1000),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def live_agent() -> revenant.Agent:
    """An agent whose process names include this test runner, so os.getpid() reads as live."""
    return revenant.CLAUDE_CODE.variant(
        process_images=revenant.CLAUDE_CODE.process_images | {"python.exe", "python", "python3"}
    )


@pytest.fixture
def with_live(root: Path) -> Path:
    _register_live(root, "11111111-1111-1111-1111-111111111111", os.getpid(), r"D:\Coding\alpha", "alpha-01")
    return root


def test_live_session_is_detected(with_live: Path, live_agent: revenant.Agent) -> None:
    sessions = {
        s.session_id[:8]: s
        for s in revenant.scan_sessions(with_live, since=revenant.parse_when("7d"), agent=live_agent)
    }
    assert sessions["11111111"].is_live
    assert sessions["11111111"].live_pid == os.getpid()
    assert sessions["11111111"].label == "alpha-01", "the running session's own name wins"
    assert not sessions["33333333"].is_live


def test_dead_pid_is_not_live(root: Path, live_agent: revenant.Agent) -> None:
    _register_live(root, "11111111-1111-1111-1111-111111111111", 999999, r"D:\Coding\alpha", "ghost")
    sessions = {
        s.session_id[:8]: s
        for s in revenant.scan_sessions(root, since=revenant.parse_when("7d"), agent=live_agent)
    }
    assert not sessions["11111111"].is_live


@pytest.mark.parametrize(
    "name,expected",
    [
        ("claude.exe", True),
        ("claude", True),
        ("node.exe", True),
        # Claude Code renames its running binary while it updates itself.
        ("claude.exe.old.1788301984027", True),
        ("claude.exe.new", True),
        ("chrome.exe", False),
        ("python.exe", False),
        ("", False),
    ],
)
def test_a_renamed_binary_is_still_the_agent(name: str, expected: bool) -> None:
    assert revenant.looks_like_agent(revenant.CLAUDE_CODE, name) is expected


def test_a_process_that_will_not_name_itself_is_held_back(
    with_live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guessing "dead" would relaunch a live session and corrupt its transcript."""
    monkeypatch.setattr(revenant, "process_states", lambda pids: {int(p): None for p in pids})
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(with_live, since=revenant.parse_when("7d"))}
    assert sessions["11111111"].is_live


def test_a_pid_with_no_process_is_not_live(with_live: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(revenant, "process_states", lambda pids: {})
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(with_live, since=revenant.parse_when("7d"))}
    assert not sessions["11111111"].is_live


def test_unrelated_process_image_is_not_live(with_live: Path) -> None:
    """This test process is python, not the agent, so the stock allowlist rejects it."""
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(with_live, since=revenant.parse_when("7d"))}
    assert not sessions["11111111"].is_live


def test_live_sessions_are_excluded_by_default(with_live: Path, live_agent: revenant.Agent) -> None:
    sessions = revenant.scan_sessions(with_live, since=revenant.parse_when("7d"), agent=live_agent)
    assert all(not s.is_live for s in revenant.filter_sessions(sessions))
    assert any(s.is_live for s in revenant.filter_sessions(sessions, include_live=True))
    assert all(s.is_live for s in revenant.filter_sessions(sessions, only_live=True))


def test_launch_refuses_running_sessions(
    with_live: Path, live_agent: revenant.Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = revenant.scan_sessions(with_live, since=revenant.parse_when("7d"), agent=live_agent)
    live = revenant.filter_sessions(sessions, only_live=True)
    assert revenant.launch(live, dry_run=False) == 2
    assert "Refusing to launch" in capsys.readouterr().out


def test_launch_without_directories_is_a_noop(capsys: pytest.CaptureFixture[str]) -> None:
    orphan = revenant.Session(session_id="abc", transcript=Path("x.jsonl"), project_slug="s", cwd=None)
    assert revenant.launch([orphan]) == 1
    assert "Nothing to launch" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #


def test_filter_by_directory(root: Path) -> None:
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    assert len(revenant.filter_sessions(sessions, dirs=["beta"])) == 1
    assert len(revenant.filter_sessions(sessions, dirs=["alpha"])) == 2
    assert len(revenant.filter_sessions(sessions, dirs=["nope"])) == 0


def test_latest_per_dir_keeps_newest(root: Path) -> None:
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    kept = revenant.filter_sessions(sessions, latest_per_dir=True)
    assert len(kept) == 2
    alpha = [s for s in kept if s.cwd == Path(r"D:\Coding\alpha")]
    assert len(alpha) == 1 and alpha[0].session_id.startswith("11111111")


def test_min_turns_drops_empty_sessions(root: Path) -> None:
    _transcript(root, "D--Coding-gamma", "55555555-5555-5555-5555-555555555555", r"D:\Coding\gamma", age_hours=1)
    _history(
        root,
        [
            ("11111111-1111-1111-1111-111111111111", "почини тесты", r"D:\Coding\alpha", 2.0),
            ("55555555-5555-5555-5555-555555555555", "/model", r"D:\Coding\gamma", 1.0),
        ],
    )
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("24h"))
    assert any(s.turns == 0 for s in sessions)
    assert all(s.turns != 0 for s in revenant.filter_sessions(sessions, min_turns=1))
    assert any(s.turns == 0 for s in revenant.filter_sessions(sessions, min_turns=0))


def test_limit(root: Path) -> None:
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    assert len(revenant.filter_sessions(sessions, limit=1)) == 1


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_render_commands_pwsh(root: Path) -> None:
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("24h")))
    text = revenant.render_commands(sessions, shell="pwsh")
    assert "cd 'D:\\Coding\\alpha'" in text
    assert "claude --resume 11111111-1111-1111-1111-111111111111" in text


def test_render_commands_escapes_quotes() -> None:
    """A directory with an apostrophe must not break out of the quoted cd."""
    session = revenant.Session(
        session_id="abc",
        transcript=Path("x.jsonl"),
        project_slug="s",
        cwd=Path("D:/it's here"),
    )
    native = str(session.cwd)
    assert f"cd '{native.replace(chr(39), chr(39) * 2)}'" in revenant.render_commands([session], shell="pwsh")
    assert f"""cd '{native.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'""" in revenant.render_commands(
        [session], shell="bash"
    )


def test_render_commands_cmd_and_bash(root: Path) -> None:
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("24h")))
    assert 'cd /d "D:\\Coding\\alpha"' in revenant.render_commands(sessions, shell="cmd")
    assert "cd 'D:\\Coding\\alpha'" in revenant.render_commands(sessions, shell="bash")


def test_render_launcher_makes_one_wt_call(root: Path) -> None:
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("7d")))
    script = revenant.render_launcher(sessions, shell="pwsh")
    assert script.count("wt.exe -w new") == 1, "all sessions must land as tabs in one window"
    assert script.count("new-tab") == len(sessions)
    assert script.count("`;") == len(sessions) - 1
    assert "Start-Process" in script, "must degrade gracefully when wt.exe is unavailable"


def test_render_launcher_handles_unknown_directory() -> None:
    session = revenant.Session(session_id="abc", transcript=Path("x.jsonl"), project_slug="s", cwd=None)
    script = revenant.render_launcher([session], shell="pwsh")
    assert "skipped abc" in script
    assert "wt.exe" not in script


def test_render_table_marks_live(
    with_live: Path, live_agent: revenant.Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = revenant.filter_sessions(
        revenant.scan_sessions(with_live, since=revenant.parse_when("7d"), agent=live_agent),
        include_live=True,
    )
    revenant.render_table(sessions)
    out = capsys.readouterr().out
    assert "held back: process" in out
    assert "D:\\Coding\\alpha" in out


def test_render_table_empty(capsys: pytest.CaptureFixture[str]) -> None:
    revenant.render_table([])
    assert "No sessions" in capsys.readouterr().out


def test_windows_terminal_argv_shape(root: Path) -> None:
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("7d")))
    plan = terminals.WindowsTerminal().plan([s.job() for s in sessions])
    assert len(plan.commands) == 1, "all tabs belong to one window"
    argv = plan.commands[0]
    assert argv[:3] == ["wt.exe", "-w", "new"]
    assert argv.count("new-tab") == len(sessions)
    assert argv.count(";") == len(sessions) - 1
    for session in sessions:
        assert session.resume_command in argv


def test_json_output_is_parseable(root: Path) -> None:
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("7d")))
    payload = json.loads(revenant.to_json(sessions))
    assert len(payload) == len(sessions)
    assert payload[0]["resume"].startswith("claude --resume ")
    assert payload[0]["agent"] == "claude-code"
    assert payload[0]["ageLabel"]


# --------------------------------------------------------------------------- #
# interactive selection
# --------------------------------------------------------------------------- #


def _fake_sessions(count: int) -> list[revenant.Session]:
    return [
        revenant.Session(
            session_id=str(i), transcript=Path(f"{i}.jsonl"), project_slug="s", cwd=Path(f"D:/p{i}")
        )
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("", ["1", "2", "3", "4"]),
        ("all", ["1", "2", "3", "4"]),
        ("1,3", ["1", "3"]),
        ("2-4", ["2", "3", "4"]),
        ("1, 3 4", ["1", "3", "4"]),
        ("q", []),
        ("9", []),
        ("garbage", []),
        ("2,2,2", ["2"]),
    ],
)
def test_pick(answer: str, expected: list[str]) -> None:
    chosen = revenant.pick(_fake_sessions(4), reader=lambda _: answer)
    assert [s.session_id for s in chosen] == expected


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #


def test_snapshot_roundtrip(with_live: Path, live_agent: revenant.Agent) -> None:
    payload = revenant.write_snapshot(with_live, agent=live_agent)
    assert len(payload["sessions"]) == 1
    assert payload["agent"] == "claude-code"
    stored = revenant.read_snapshot(with_live, agent=live_agent)
    assert stored is not None
    assert stored["sessions"][0]["cwd"] == r"D:\Coding\alpha"


def test_read_snapshot_missing(root: Path) -> None:
    assert revenant.read_snapshot(root) is None


def test_snapshot_is_atomic(with_live: Path, live_agent: revenant.Agent) -> None:
    revenant.write_snapshot(with_live, agent=live_agent)
    assert not list(revenant.snapshot_path(with_live).parent.glob("*.tmp"))


def test_snapshots_do_not_leak_between_roots(
    with_live: Path, live_agent: revenant.Agent, tmp_path: Path
) -> None:
    """Two config roots must not share one snapshot file."""
    other = tmp_path / "other-config"
    (other / "projects").mkdir(parents=True)
    revenant.write_snapshot(with_live, agent=live_agent)
    assert revenant.snapshot_path(with_live) != revenant.snapshot_path(other)
    assert revenant.read_snapshot(other) is None


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def test_cli_list(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revenant.main(["--root", str(root), "--since", "7d"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out


def test_cli_json_is_pure(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revenant.main(["--root", str(root), "--since", "7d", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_json_stays_pure_with_snapshot(
    with_live: Path, live_agent: revenant.Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    revenant.write_snapshot(with_live, agent=live_agent)
    assert revenant.main(["--root", str(with_live), "--since", "7d", "--from-snapshot", "--include-live", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_cli_emit(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "out" / "revive.ps1"
    assert revenant.main(["--root", str(root), "--since", "7d", "--emit", str(target)]) == 0
    assert "claude --resume" in target.read_text(encoding="utf-8")


def test_cli_emit_picks_shell_from_extension(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "revive.sh"
    revenant.main(["--root", str(root), "--since", "7d", "--emit", str(target)])
    assert target.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_cli_snapshot_command(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revenant.main(["snapshot", "--root", str(root)]) == 0
    assert "Recorded 0 running session" in capsys.readouterr().out


def test_cli_missing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revenant.main(["--root", str(tmp_path / "nope")]) == 1
    assert "not found" in capsys.readouterr().out


def test_cli_dry_run_launch(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revenant.main(["--root", str(root), "--since", "7d", "--launch", "--dry-run"]) == 0
    assert "wt.exe" in capsys.readouterr().out


def test_cli_rejects_unknown_agent(root: Path) -> None:
    with pytest.raises(SystemExit):
        revenant.main(["--root", str(root), "--agent", "nope"])


def test_cli_never_writes_to_agent_state(root: Path) -> None:
    """The whole point: reading sessions must not mutate anything the agent owns."""
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    revenant.main(["--root", str(root), "--since", "7d", "--print"])
    after = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------- #
# regressions
# --------------------------------------------------------------------------- #


def test_cmd_launcher_uses_quotes_cmd_understands(root: Path) -> None:
    """`repr()` emitted a single-quoted, escaped path that `cd /d` cannot use."""
    sessions = revenant.filter_sessions(revenant.scan_sessions(root, since=revenant.parse_when("24h")))
    script = revenant.render_launcher(sessions, shell="cmd")
    assert '/D "D:\\Coding\\alpha"' in script
    assert "'D:" not in script


def test_bad_time_window_is_a_usage_error_not_a_traceback(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert revenant.main(["--root", str(root), "--since", "bogus"]) == 2
    assert "cannot read time" in capsys.readouterr().out


def test_from_snapshot_without_a_snapshot_restores_nothing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Falling back to the whole window would relaunch sessions nobody recorded."""
    assert revenant.main(["--root", str(root), "--since", "7d", "--from-snapshot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_corrupt_pid_does_not_abort_the_scan(root: Path) -> None:
    (root / "sessions" / "bad.json").write_text(
        json.dumps({"pid": "not-a-number", "sessionId": "11111111-1111-1111-1111-111111111111"}),
        encoding="utf-8",
    )
    sessions = revenant.scan_sessions(root, since=revenant.parse_when("7d"))
    assert len(sessions) == 3
    assert all(not s.is_live for s in sessions)


def test_a_truncated_tail_scan_does_not_invent_a_first_prompt(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the tail window the earliest prompt read is not the session's first."""
    (root / "history.jsonl").unlink()
    monkeypatch.setattr(agents, "TAIL_BYTES", 250)
    sessions = {s.session_id[:8]: s for s in revenant.scan_sessions(root, since=revenant.parse_when("7d"))}
    alpha = sessions["11111111"]
    assert alpha.first_prompt == ""
    assert alpha.turns is None, "an incomplete tail is a lower bound, not a count"
    assert alpha.last_prompt == "real question about the code"
