"""Session names. The whole point of the list is telling one row from another,
and the last prompt is usually "continue"."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revenant  # noqa: E402
import revenant_agents as agents  # noqa: E402

CLAUDE = agents.AGENTS["claude-code"]
CODEX = agents.AGENTS["codex"]


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_a_generated_title_is_read_from_the_transcript(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "ai-title", "aiTitle": "Refactor the payment retry loop", "sessionId": "s"},
        ],
    )
    assert CLAUDE.title(path) == "Refactor the payment retry loop"


def test_a_name_the_user_set_beats_a_generated_one(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            {"type": "ai-title", "aiTitle": "Something the model guessed", "sessionId": "s"},
            {"type": "custom-title", "customTitle": "auth-refactor", "sessionId": "s"},
        ],
    )
    assert CLAUDE.title(path) == "auth-refactor"


def test_the_last_title_written_is_the_current_one(tmp_path: Path) -> None:
    """Claude Code rewrites these records as the session goes on."""
    path = _write(
        tmp_path / "s.jsonl",
        [
            {"type": "custom-title", "customTitle": "first name", "sessionId": "s"},
            {"type": "custom-title", "customTitle": "renamed later", "sessionId": "s"},
        ],
    )
    assert CLAUDE.title(path) == "renamed later"


def test_a_session_with_no_name_reports_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "hello"}}])
    assert CLAUDE.title(path) == ""


def test_a_missing_or_corrupt_transcript_is_not_an_error(tmp_path: Path) -> None:
    assert CLAUDE.title(tmp_path / "nothing.jsonl") == ""
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(b'{"type": "ai-title", "aiTitle": "trunc\xff\xfe\n{not json at all\n')
    assert CLAUDE.title(broken) == ""


def test_a_title_only_written_early_is_still_found(tmp_path: Path) -> None:
    """The window is read from the end, so a long session must not lose its name."""
    filler = {"type": "assistant", "message": {"content": "x" * 400}}
    records = [{"type": "custom-title", "customTitle": "early name", "sessionId": "s"}]
    records += [filler] * 200
    path = _write(tmp_path / "s.jsonl", records)
    assert path.stat().st_size < agents.TAIL_BYTES
    assert CLAUDE.title(path) == "early name"


def test_codex_reads_thread_names_from_its_index(tmp_path: Path) -> None:
    _write(
        tmp_path / "session_index.jsonl",
        [
            {"id": "aaa", "thread_name": "Check the training run", "updated_at": "2026-04-24T16:34:00Z"},
            {"id": "bbb", "thread_name": "  ", "updated_at": "2026-04-24T16:34:00Z"},
        ],
    )
    found = CODEX.titles(tmp_path)
    assert found == {"aaa": "Check the training run"}


def test_the_config_root_is_recovered_from_a_transcript_path() -> None:
    """`--root` means the agent's default directory is the wrong place to look."""
    claude = Path("/backup/claude/projects/some-slug/id.jsonl")
    assert CLAUDE.root_of(claude) == Path("/backup/claude")
    codex = Path("/backup/codex/sessions/2026/04/24/rollout-2026-04-24T19-08-07-id.jsonl")
    assert CODEX.root_of(codex) == Path("/backup/codex")


def _session(**kwargs) -> revenant.Session:
    base = dict(session_id="s", transcript=Path("s.jsonl"), project_slug="slug")
    return revenant.Session(**{**base, **kwargs})


def test_a_title_describes_the_row_better_than_the_last_prompt() -> None:
    session = _session(cwd=Path("/home/me/payments"), last_prompt="continue", title="Fix the retry loop")
    assert session.summary == "Fix the retry loop"


def test_a_title_that_only_repeats_the_directory_gives_way_to_the_prompt() -> None:
    """Two columns saying "payments" tell you half as much as one."""
    session = _session(cwd=Path("/home/me/payments"), last_prompt="drop the index", title="payments")
    assert session.label == "payments"
    assert session.summary == "drop the index"


def test_a_repeated_title_still_shows_when_there_is_no_prompt() -> None:
    session = _session(cwd=Path("/home/me/payments"), title="Payments")
    assert session.summary == "Payments"


def test_naming_reads_each_root_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is per config directory, not per session."""
    calls: list[Path] = []
    monkeypatch.setattr(type(CODEX), "titles", lambda self, root: calls.append(root) or {})
    sessions = [
        _session(session_id=str(n), agent=CODEX, transcript=tmp_path / "sessions/y/m/d/r.jsonl")
        for n in range(3)
    ]
    revenant.name_sessions(sessions)
    assert calls == [tmp_path]
