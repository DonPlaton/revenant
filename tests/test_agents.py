"""Agent readers. Each one is exercised against a synthetic config directory."""

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


NOW = datetime.now(timezone.utc)
SESSION_A = "01a00718-2757-7f42-b937-58d92827df7c"
SESSION_B = "01a00719-1111-7f42-b937-58d92827df7d"


def _rollout(root: Path, session_id: str, cwd: str, prompts: list[str], *, age_hours: float) -> Path:
    started = NOW - timedelta(hours=age_hours)
    day = started.strftime("%Y/%m/%d")
    directory = root / "sessions" / day
    directory.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y-%m-%dT%H-%M-%S")
    path = directory / f"rollout-{stamp}-{session_id}.jsonl"

    lines = [
        {
            "timestamp": started.isoformat().replace("+00:00", "Z"),
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "cwd": cwd,
                "cli_version": "0.147.0",
                "timestamp": started.isoformat().replace("+00:00", "Z"),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nignore me"}],
            },
        },
    ]
    for prompt in prompts:
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
        lines.append({"type": "response_item", "payload": {"type": "message", "role": "assistant"}})

    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8"
    )
    os.utime(path, (started.timestamp(), started.timestamp()))
    return path


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    root = tmp_path / ".codex"
    _rollout(root, SESSION_A, r"D:\dev\payments", ["fix the retry path", "now add a test"], age_hours=3)
    _rollout(root, SESSION_B, "/home/me/ml", ["why does it OOM"], age_hours=40)
    (root / "history.jsonl").write_text(
        "\n".join(
            json.dumps({"session_id": SESSION_A, "ts": int((NOW - timedelta(hours=3)).timestamp()), "text": t})
            for t in ("fix the retry path", "/model", "now add a test")
        )
        + "\n",
        encoding="utf-8",
    )
    return root


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def test_both_agents_are_registered() -> None:
    assert set(agents.AGENTS) == {"claude-code", "codex"}
    assert agents.DEFAULT_AGENT.key == "claude-code"


def test_unknown_agent_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        agents.get_agent("emacs")
    assert "Unknown agent" in str(caught.value)


def test_every_agent_has_a_distinct_home_and_resume_command() -> None:
    homes = [agent.home_relative for agent in agents.AGENTS.values()]
    assert len(homes) == len(set(homes))
    for agent in agents.AGENTS.values():
        assert "{id}" in agent.resume_template


def test_config_dir_follows_the_agents_own_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert agents.AGENTS["codex"].config_dir() == tmp_path / "elsewhere"


def test_variant_copies_without_touching_the_original() -> None:
    original = agents.AGENTS["claude-code"]
    widened = original.variant(process_images={"python.exe"})
    assert widened.process_images == frozenset({"python.exe"})
    assert "claude.exe" in original.process_images


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #


def test_codex_finds_rollouts_and_reads_the_directory(codex_root: Path) -> None:
    codex = agents.AGENTS["codex"]
    found = sorted(codex.transcripts(codex_root))
    assert len(found) == 2
    head = codex.head(next(p for p in found if SESSION_A in p.name))
    assert head["cwd"] == r"D:\dev\payments"
    assert head["version"] == "0.147.0"
    assert head["started"] is not None


def test_codex_session_id_comes_from_the_filename(codex_root: Path) -> None:
    codex = agents.AGENTS["codex"]
    path = next(p for p in codex.transcripts(codex_root) if SESSION_A in p.name)
    assert codex.session_id(path) == SESSION_A
    assert codex.resume_command(SESSION_A) == f"codex resume {SESSION_A}"


def test_codex_skips_the_replayed_instructions(codex_root: Path) -> None:
    codex = agents.AGENTS["codex"]
    path = next(p for p in codex.transcripts(codex_root) if SESSION_A in p.name)
    first, last, count, complete = codex.tail(path)
    assert complete
    assert first == "fix the retry path", "the AGENTS.md replay is not a user prompt"
    assert last == "now add a test"
    assert count == 2


def test_codex_history_index(codex_root: Path) -> None:
    index = agents.AGENTS["codex"].history(codex_root)
    assert set(index) == {SESSION_A}
    assert [text for _, text, _ in index[SESSION_A]] == [
        "fix the retry path",
        "/model",
        "now add a test",
    ]


def test_codex_sessions_scan_end_to_end(codex_root: Path) -> None:
    codex = agents.AGENTS["codex"]
    found = revenant.scan_sessions(codex_root, since=revenant.parse_when("7d"), agent=codex)
    assert len(found) == 2
    newest = found[0]
    assert newest.session_id == SESSION_A
    assert newest.cwd == Path(r"D:\dev\payments")
    assert newest.turns == 2, "slash commands do not count"
    assert newest.last_prompt == "now add a test"
    assert newest.resume_command.startswith("codex resume ")


def test_codex_holds_back_a_session_touched_moments_ago(codex_root: Path) -> None:
    """Codex keeps no registry, so recent activity is the only liveness signal."""
    fresh = _rollout(codex_root, "01a0071a-2222-7f42-b937-58d92827df7e", "/home/me/hot", ["go"], age_hours=0)
    os.utime(fresh, None)
    found = {s.session_id: s for s in revenant.scan_sessions(codex_root, since=revenant.parse_when("7d"), agent=agents.AGENTS["codex"])}
    hot = found["01a0071a-2222-7f42-b937-58d92827df7e"]
    assert hot.is_live
    assert hot.live_reason == "active moments ago"
    assert not found[SESSION_A].is_live


def test_a_cold_codex_session_is_revivable(codex_root: Path) -> None:
    found = revenant.scan_sessions(codex_root, since=revenant.parse_when("7d"), agent=agents.AGENTS["codex"])
    assert all(not s.is_live for s in found)
    assert len(revenant.filter_sessions(found)) == 2


def test_codex_survives_a_rollout_without_meta(codex_root: Path) -> None:
    broken = codex_root / "sessions" / "2026" / "01" / "01"
    broken.mkdir(parents=True)
    (broken / "rollout-2026-01-01T00-00-00-0000ffff-3333-7f42-b937-58d92827df7f.jsonl").write_text(
        "not json\n", encoding="utf-8"
    )
    found = revenant.scan_sessions(codex_root, since=revenant.parse_when("all"), agent=agents.AGENTS["codex"])
    assert len(found) == 3
    assert any(s.cwd is None for s in found)


# --------------------------------------------------------------------------- #
# scanning several agents at once
# --------------------------------------------------------------------------- #


def test_scan_all_merges_and_sorts(codex_root: Path, tmp_path: Path) -> None:
    claude_root = tmp_path / ".claude"
    (claude_root / "projects" / "d--dev-web").mkdir(parents=True)
    transcript = claude_root / "projects" / "d--dev-web" / "44444444-4444-4444-4444-444444444444.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "/home/me/web",
                "timestamp": NOW.isoformat().replace("+00:00", "Z"),
                "message": {"role": "user", "content": "ship it"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    claude = agents.AGENTS["claude-code"].variant()
    codex = agents.AGENTS["codex"].variant()
    claude.config_dir = lambda explicit=None: claude_root  # type: ignore[method-assign]
    codex.config_dir = lambda explicit=None: codex_root  # type: ignore[method-assign]

    merged = revenant.scan_all(since=revenant.parse_when("7d"), agents=[claude, codex])
    assert {s.agent.key for s in merged} == {"claude-code", "codex"}
    ages = [s.last_active for s in merged]
    assert ages == sorted(ages, reverse=True)
    assert merged[0].agent.key == "claude-code"


def test_the_table_names_the_agent_when_several_are_mixed(
    codex_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codex = agents.AGENTS["codex"]
    found = revenant.scan_sessions(codex_root, since=revenant.parse_when("7d"), agent=codex)
    claude_like = revenant.Session(
        session_id="x", transcript=Path("x.jsonl"), project_slug="s",
        agent=agents.AGENTS["claude-code"], cwd=Path("/home/me/web"), last_active=NOW,
    )
    revenant.render_table([claude_like, *found])
    out = capsys.readouterr().out
    assert "AGENT" in out
    assert "Codex" in out and "Claude Code" in out
