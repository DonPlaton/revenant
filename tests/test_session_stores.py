"""Discovery regressions for desktop Codex and OpenCode's shared session store."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import revenant
import revenant_agents as agents
import revenant_gui as gui
from test_agents import SESSION_A, _rollout

NOW = datetime.now(timezone.utc)
COLD = NOW - timedelta(hours=3)
SINCE = NOW - timedelta(days=7)


def _opencode_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "opencode.db")
    connection.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
            directory TEXT, title TEXT, version TEXT, time_created INTEGER,
            time_updated INTEGER, time_archived INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    return connection


def _session(connection: sqlite3.Connection, session_id: str = "ses_parent", *,
             when: datetime = COLD, parent: str | None = None, archived: bool = False,
             prompts: tuple[str, ...] = ("fix the parser", "add a regression test")) -> None:
    timestamp = int(when.timestamp() * 1000)
    connection.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (session_id, "project", parent, "D:/Coding/Bug_Bounty", "Parser repair", "1.18.34",
                        timestamp - 1000, timestamp, timestamp if archived else None))
    for i, prompt in enumerate(prompts):
        message_id = f"{session_id}_msg{i}"
        connection.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                           (message_id, session_id, timestamp + i, timestamp + i,
                            json.dumps({"role": "user"})))
        connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                           (f"{message_id}_part", message_id, session_id, timestamp + i, timestamp + i,
                            json.dumps({"type": "text", "text": prompt})))
    connection.commit()


def _scan(root: Path, key: str = "opencode", **kwargs) -> list[revenant.Session]:
    return revenant.scan_sessions(root, since=kwargs.pop("since", SINCE),
                                  agent=agents.AGENTS[key], now=NOW, **kwargs)


def _codex_db(root: Path, transcript: Path, **changes) -> None:
    values = {"id": SESSION_A, "rollout_path": str(transcript), "cwd": "D:/Coding/new-location",
              "title": "Generated title", "name": "My renamed thread", "cli_version": "0.147.0",
              "created_at": int(COLD.timestamp()) - 500, "updated_at": int(COLD.timestamp()),
              "first_user_message": "find the missing thread", "git_branch": "main"}
    values.update(changes)
    with sqlite3.connect(root / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, "
                           "title TEXT, name TEXT, cli_version TEXT, created_at INTEGER, updated_at INTEGER, "
                           "first_user_message TEXT, git_branch TEXT)")
        connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(values.values()))


def test_opencode_database_reads_directory_title_turns_and_resume(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
    found = _scan(tmp_path)
    assert len(found) == 1
    session = found[0]
    assert session.cwd == Path("D:/Coding/Bug_Bounty")
    assert session.title == "Parser repair"
    assert session.turns == 2
    assert session.first_prompt == "fix the parser"
    assert session.last_prompt == "add a regression test"
    assert session.last_active == COLD.replace(microsecond=(COLD.microsecond // 1000) * 1000)
    assert session.resume_command == "opencode --session ses_parent"
    assert not session.is_live, "the shared database was just written, but this session is cold"


def test_opencode_includes_child_and_archived_sessions(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
        _session(connection, "ses_child", parent="ses_parent")
        _session(connection, "ses_archive", archived=True)
    assert {s.session_id for s in _scan(tmp_path)} == {"ses_parent", "ses_child", "ses_archive"}


def test_opencode_filters_by_session_activity_not_database_mtime(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
        _session(connection, "ses_old", when=NOW - timedelta(days=30))
        _session(connection, "ses_hot", when=NOW)
    assert {s.session_id for s in _scan(tmp_path, until=NOW - timedelta(hours=1))} == {"ses_parent"}
    found = _scan(tmp_path)
    assert next(s for s in found if s.session_id == "ses_hot").is_live
    assert {s.session_id for s in revenant.filter_sessions(found)} == {"ses_parent"}


def test_opencode_liveness_refresh_does_not_hold_every_session(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
        _session(connection, "ses_other")
        found = _scan(tmp_path)
        connection.execute("UPDATE session SET time_updated = ? WHERE id = 'ses_other'",
                           (int(datetime.now(timezone.utc).timestamp() * 1000),))
        connection.commit()
        revenant.refresh_liveness(found, lambda _: tmp_path)
    assert not next(s for s in found if s.session_id == "ses_parent").is_live
    assert next(s for s in found if s.session_id == "ses_other").is_live


def test_opencode_ignores_tools_synthetic_parts_and_damaged_json(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection, prompts=("real prompt",))
        for i, data in enumerate(({"type": "text", "text": "injected", "synthetic": True},
                                  {"type": "text", "text": "ignored", "ignored": True},
                                  {"type": "tool", "text": "tool output"}, "broken")):
            connection.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                               (f"extra{i}", "ses_parent_msg0", "ses_parent", 0, 0,
                                data if isinstance(data, str) else json.dumps(data)))
        connection.execute("INSERT INTO message VALUES ('bad', 'ses_parent', 0, 0, '{')")
        connection.execute("INSERT INTO part VALUES ('badpart', 'bad', 'ses_parent', 0, 0, '{}')")
    assert _scan(tmp_path)[0].last_prompt == "real prompt"
    assert _scan(tmp_path)[0].turns == 1


def test_opencode_empty_and_slash_only_sessions_are_filtered(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection, prompts=())
        _session(connection, "ses_slash", prompts=("/model",))
    assert revenant.filter_sessions(_scan(tmp_path)) == []


def test_opencode_rejects_session_ids_that_could_inject_shell_commands(tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection, "ses_bad; touch danger")
    assert _scan(tmp_path) == []


def _legacy(root: Path, session_id: str = "ses_legacy") -> Path:
    path = root / "storage/session/project" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": session_id, "directory": "D:/Coding/Bug_Bounty", "title": "Legacy task",
                                "time": {"created": COLD.timestamp() * 1000, "updated": COLD.timestamp() * 1000}}),
                    encoding="utf-8")
    message = root / "storage/message" / session_id / "msg_001.json"
    message.parent.mkdir(parents=True, exist_ok=True)
    message.write_text('{"role": "user"}', encoding="utf-8")
    part = root / "storage/part/msg_001/prt_001.json"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text('{"type": "text", "text": "recover my work"}', encoding="utf-8")
    return path


def test_opencode_legacy_store_and_migration_deduplication(tmp_path: Path) -> None:
    _legacy(tmp_path)
    assert _scan(tmp_path)[0].last_prompt == "recover my work"
    assert _scan(tmp_path)[0].title == "Legacy task"
    with _opencode_db(tmp_path) as connection:
        _session(connection, "ses_legacy")
    found = _scan(tmp_path)
    assert len(found) == 1
    assert found[0].title == "Parser repair"


@pytest.mark.parametrize("contents", [b"corrupt database", b""])
def test_opencode_bad_database_falls_back_to_legacy(tmp_path: Path, contents: bytes) -> None:
    _legacy(tmp_path)
    (tmp_path / "opencode.db").write_bytes(contents)
    assert len(_scan(tmp_path)) == 1


def test_opencode_non_utf8_legacy_file_does_not_abort_scan(tmp_path: Path) -> None:
    _legacy(tmp_path)
    broken = tmp_path / "storage/session/project/ses_broken.json"
    broken.write_bytes(b"\xff\xfe\xff")
    assert {s.session_id for s in revenant.filter_sessions(_scan(tmp_path))} == {"ses_legacy"}


def test_opencode_uses_xdg_data_home_and_explicit_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = agents.OpenCode()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert agent.config_dir() == tmp_path / "opencode"
    assert agent.config_dir(tmp_path / "explicit") == tmp_path / "explicit"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert agent.config_dir() == tmp_path / ".local/share/opencode"


def test_sqlite_reads_uncheckpointed_wal_without_changing_data(tmp_path: Path) -> None:
    connection = _opencode_db(tmp_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        _session(connection)
        paths = [tmp_path / "opencode.db", tmp_path / "opencode.db-wal"]
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        assert _scan(tmp_path)[0].turns == 2
        assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        assert agents._query(paths[0], "DELETE FROM session") == []
        assert _scan(tmp_path)[0].session_id == "ses_parent"
    finally:
        connection.close()


def test_scan_never_creates_a_missing_database(tmp_path: Path) -> None:
    assert _scan(tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_codex_finds_flat_nested_and_archived_rollouts(tmp_path: Path) -> None:
    path = _rollout(tmp_path, SESSION_A, "/work/project", ["restore this task"], age_hours=3)
    flat = tmp_path / "archived_sessions" / path.name
    flat.parent.mkdir()
    path.rename(flat)
    os.utime(flat, (COLD.timestamp(), COLD.timestamp()))
    assert agents.Codex().root_of(flat) == tmp_path
    assert _scan(tmp_path, "codex")[0].session_id == SESSION_A


def test_codex_reads_database_paths_metadata_and_renamed_titles(tmp_path: Path) -> None:
    path = tmp_path / "imported.jsonl"
    path.write_text('{"type":"response_item","payload":{"type":"message","role":"user",'
                    '"content":[{"type":"input_text","text":"fix this"}]}}\n', encoding="utf-8")
    os.utime(path, (COLD.timestamp(), COLD.timestamp()))
    _codex_db(tmp_path, path)
    found = _scan(tmp_path, "codex")
    assert len(found) == 1
    assert found[0].session_id == SESSION_A
    assert found[0].cwd == Path("D:/Coding/new-location")
    assert found[0].title == "My renamed thread"
    assert found[0].git_branch == "main"
    assert found[0].last_prompt == "fix this"


def test_codex_deduplicates_database_and_rollout_and_uses_first_message_fallback(tmp_path: Path) -> None:
    path = _rollout(tmp_path, SESSION_A, "/work/project", [], age_hours=3)
    _codex_db(tmp_path, path)
    found = _scan(tmp_path, "codex")
    assert len(found) == 1
    assert found[0].last_prompt == "find the missing thread"
    assert found[0].turns is None
    assert revenant.filter_sessions(found)


def test_codex_bad_or_newer_incompatible_database_does_not_hide_transcripts(tmp_path: Path) -> None:
    path = _rollout(tmp_path, SESSION_A, "/work/project", ["fix it"], age_hours=3)
    _codex_db(tmp_path, path)
    (tmp_path / "state_6.sqlite").write_bytes(b"broken")
    assert _scan(tmp_path, "codex")[0].title == "My renamed thread"


def test_codex_rejects_unsafe_database_thread_ids(tmp_path: Path) -> None:
    path = tmp_path / "imported.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    _codex_db(tmp_path, path, id="thread; echo injected")
    assert _scan(tmp_path, "codex") == []


def test_codex_rechecks_database_activity_before_resume(tmp_path: Path) -> None:
    path = _rollout(tmp_path, SESSION_A, "/work/project", ["fix it"], age_hours=3)
    _codex_db(tmp_path, path)
    found = _scan(tmp_path, "codex")
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET updated_at = ?", (int(datetime.now(timezone.utc).timestamp()),))
    revenant.refresh_liveness(found, lambda _: tmp_path)
    assert found[0].is_live


def test_codex_sparse_history_does_not_mask_newer_rollout_prompts(tmp_path: Path) -> None:
    _rollout(tmp_path, SESSION_A, "/work/project", ["first prompt", "new desktop turn"], age_hours=3)
    (tmp_path / "history.jsonl").write_text(json.dumps({"session_id": SESSION_A, "text": "first prompt",
                                                       "ts": (COLD - timedelta(hours=1)).timestamp()}) + "\n",
                                            encoding="utf-8")
    found = _scan(tmp_path, "codex")[0]
    assert found.last_prompt == "new desktop turn"
    assert found.turns == 2


def test_codex_projected_history_replaces_sparse_cli_index(tmp_path: Path) -> None:
    _rollout(tmp_path, SESSION_A, "/work/project", [], age_hours=3)
    (tmp_path / "history.jsonl").write_text(json.dumps({"session_id": SESSION_A, "text": "stale CLI prompt",
                                                       "ts": (COLD - timedelta(days=1)).timestamp()}) + "\n",
                                            encoding="utf-8")
    with sqlite3.connect(tmp_path / "thread_history_1.sqlite") as connection:
        connection.execute("CREATE TABLE thread_items (thread_id TEXT, created_at_ms INTEGER, "
                           "item_json TEXT, item_type TEXT, rollout_ordinal INTEGER)")
        for i, text in enumerate(("first desktop prompt", "latest desktop prompt")):
            connection.execute("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)",
                               (SESSION_A, int(COLD.timestamp() * 1000) + i,
                                json.dumps({"type": "userMessage", "content": [{"type": "text", "text": text}]}),
                                "userMessage", i))
        connection.execute("INSERT INTO thread_items VALUES (?, 0, '{', 'userMessage', 3)", (SESSION_A,))
    found = _scan(tmp_path, "codex")[0]
    assert found.turns == 2
    assert found.first_prompt == "first desktop prompt"
    assert found.last_prompt == "latest desktop prompt"


def test_codex_multipart_prompt_keeps_user_text_after_instructions() -> None:
    record = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "# AGENTS.md instructions\ninternal instructions"},
        {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
        {"type": "input_text", "text": "recover my actual task"},
    ]}}
    assert agents.Codex()._prompt_of(record) == "recover my actual task"


def test_codex_repeated_turns_are_counted_but_event_response_pairs_are_not(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    pair = [{"type": "event_msg", "payload": {"type": "user_message", "message": "continue"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                    "content": [{"type": "input_text", "text": "continue"}]}}]
    path.write_text("\n".join(json.dumps(record) for record in pair * 3) + "\n", encoding="utf-8")
    assert agents.Codex().tail(path)[2:] == (3, True)


def test_gui_opens_all_agents_and_can_copy_opencode_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    with _opencode_db(root) as connection:
        _session(connection)
    codex_root = tmp_path / "codex"
    _rollout(codex_root, SESSION_A, "/work/project", ["fix it"], age_hours=3)
    monkeypatch.setattr(agents.OpenCode, "config_dir", lambda self, explicit=None: root)
    monkeypatch.setattr(agents.Codex, "config_dir", lambda self, explicit=None: codex_root)
    monkeypatch.setattr(agents, "installed_agents", lambda: [agents.Codex(), agents.OpenCode()])
    backend = gui.Backend()
    monkeypatch.setattr(backend, "layouts", lambda: {})
    payload = backend.sessions(days=7)
    assert payload["agent"] == "all"
    assert {s["agent"] for s in payload["sessions"]} == {"codex", "opencode"}
    assert "opencode --session ses_parent" in backend.commands(["ses_parent"], days=7)["text"]
    assert backend.sessions(days=7, which="opencode")["agent"] == "opencode"


def test_cli_opencode_json_and_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
    assert revenant.main(["--agent", "opencode", "--root", str(tmp_path), "--since", "7d", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["agent"] == "opencode"
    assert revenant.main(["--agent", "opencode", "--root", str(tmp_path), "--print"]) == 0
    assert "opencode --session ses_parent" in capsys.readouterr().out


def test_gui_explicit_agent_keeps_its_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agents, "installed_agents", lambda: [agents.Codex(), agents.OpenCode()])
    backend = gui.Backend(agent=agents.Codex(), combined=False)
    assert backend.default_key() == "codex"


def test_gui_refuses_opencode_session_resumed_after_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _opencode_db(tmp_path) as connection:
        _session(connection)
        backend = gui.Backend(agent=agents.OpenCode(), root=str(tmp_path))
        monkeypatch.setattr(backend, "layouts", lambda: {})
        assert backend.sessions(days=7)["sessions"][0]["live"] is False
        connection.execute("UPDATE session SET time_updated = ?",
                           (int(datetime.now(timezone.utc).timestamp() * 1000),))
        connection.commit()
        result = backend.revive(["ses_parent"], days=7)
    assert result["ok"] is False
    assert result["count"] == 0
    assert "still running" in result["message"]
