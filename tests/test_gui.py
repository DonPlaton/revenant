"""Tests for the desktop app's local backend, mostly about what it refuses to do."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revenant_gui as gui  # noqa: E402
import revenant  # noqa: E402

from test_revenant import _history, _transcript  # noqa: E402


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A running backend over a synthetic config root; torn down after each test."""
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir(parents=True)
    _transcript(root, "D--Coding-alpha", "11111111-1111-1111-1111-111111111111", r"D:\Coding\alpha", age_hours=2)
    _history(root, [("11111111-1111-1111-1111-111111111111", "fix the parser", r"D:\Coding\alpha", 2.0)])
    monkeypatch.setattr(revenant, "state_dir", lambda: tmp_path / "state")

    backend = gui.Backend(root=str(root))
    server, url = gui.serve(backend)
    base = url.split("/?")[0]
    try:
        yield backend, base
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _post(url: str, payload: dict, *, headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# access control
# --------------------------------------------------------------------------- #


def test_requests_without_a_token_are_refused(served) -> None:
    _, base = served
    assert _get(f"{base}/api/sessions?days=7")[0] == 403
    assert _get(f"{base}/")[0] == 403


def test_a_wrong_token_is_refused(served) -> None:
    _, base = served
    assert _get(f"{base}/api/sessions?days=7&t=not-the-token")[0] == 403


def test_the_right_token_is_accepted(served) -> None:
    backend, base = served
    status, body = _get(f"{base}/api/sessions?days=7&t={backend.token}")
    assert status == 200
    payload = json.loads(body)
    assert payload["error"] is None
    assert [s["label"] for s in payload["sessions"]] == ["alpha"]
    assert payload["sessions"][0]["lastPrompt"] == "fix the parser"


def test_a_token_in_the_header_also_works(served) -> None:
    backend, base = served
    status, _ = _post(
        f"{base}/api/commands", {"ids": [], "days": 7}, headers={"X-Revenant-Token": backend.token}
    )
    assert status == 200


def test_a_foreign_host_header_is_refused(served) -> None:
    """Blocks DNS rebinding: a page resolving some name to 127.0.0.1 cannot drive the app."""
    backend, base = served
    port = base.rsplit(":", 1)[1]
    request = urllib.request.Request(f"{base}/api/sessions?days=7&t={backend.token}")
    request.add_header("Host", f"evil.example:{port}")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403


def test_static_files_cannot_escape_the_ui_directory(served) -> None:
    backend, base = served
    assert _get(f"{base}/ui/../revenant.py?t={backend.token}")[0] == 404
    assert _get(f"{base}/ui/../../../Windows/win.ini?t={backend.token}")[0] == 404


def test_index_is_served_with_the_token(served) -> None:
    backend, base = served
    status, body = _get(f"{base}/?t={backend.token}")
    assert status == 200
    assert "REVENANT" in body


def test_unknown_routes_are_404(served) -> None:
    backend, base = served
    assert _get(f"{base}/api/nope?t={backend.token}")[0] == 404
    assert _post(f"{base}/api/nope?t={backend.token}", {})[0] == 404


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #


def test_reviving_an_unknown_id_is_a_clean_failure(served) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/revive?t={backend.token}", {"ids": ["nope"], "days": 7})
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is False and payload["count"] == 0


def test_commands_endpoint_returns_paste_ready_text(served) -> None:
    backend, base = served
    status, body = _post(
        f"{base}/api/commands?t={backend.token}",
        {"ids": ["11111111-1111-1111-1111-111111111111"], "days": 7},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["count"] == 1
    assert "claude --resume 11111111-1111-1111-1111-111111111111" in payload["text"]
    assert "cd 'D:\\Coding\\alpha'" in payload["text"]


def test_reveal_rejects_a_missing_folder(served) -> None:
    backend, base = served
    # ALPHA's folder is on record but not on this disk.
    status, body = _post(f"{base}/api/reveal?t={backend.token}", {"ids": [ALPHA], "days": 7})
    assert status == 200
    assert json.loads(body)["ok"] is False


def test_reveal_opens_only_a_folder_the_scan_found(served, tmp_path: Path, monkeypatch) -> None:
    """The page names a session, never a path, so a request cannot point the
    file manager anywhere the scan did not find a session."""
    backend, base = served
    here = tmp_path / "project"
    here.mkdir()
    sid = "33333333-3333-3333-3333-333333333333"
    _transcript(backend.root, "C--project", sid, str(here), age_hours=1)
    backend.invalidate()
    opened: list[str] = []
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda target: opened.append(str(target)))
    else:
        monkeypatch.setattr(gui.subprocess, "Popen", lambda argv, **_: opened.append(argv[-1]))
    for payload in ({"path": str(tmp_path)}, {"ids": ["not-a-session"], "days": 7}):
        _, body = _post(f"{base}/api/reveal?t={backend.token}", payload)
        assert json.loads(body)["ok"] is False
    _, body = _post(f"{base}/api/reveal?t={backend.token}", {"ids": [sid], "days": 7})
    assert json.loads(body)["ok"] is True
    assert opened == [str(here)]


def test_only_the_pages_own_files_are_served(served) -> None:
    backend, base = served
    assert _get(f"{base}/ui/index.html?t={backend.token}")[0] == 200
    assert set(gui.UI_FILES) == {"index.html"}
    for name in ("..%2Frevenant.py", "..%5Crevenant.py", "%2e%2e/revenant.py", "nothing.html"):
        assert _get(f"{base}/ui/{name}?t={backend.token}")[0] == 404


def test_missing_config_dir_is_reported_not_raised(tmp_path: Path) -> None:
    backend = gui.Backend(root=str(tmp_path / "nowhere"))
    payload = backend.sessions(days=7)
    assert payload["sessions"] == []
    assert "not found" in payload["error"]


def test_oversized_bodies_are_ignored(served) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/commands?t={backend.token}", {"ids": ["x" * (300 * 1024)], "days": 7})
    assert status == 200
    assert json.loads(body)["count"] == 0


@pytest.mark.parametrize(
    "value,expected",
    [("7", 7.0), ("0", 0.04), ("99999", 3650.0), ("abc", 3.0), (None, 3.0), (-5, 0.04)],
)
def test_day_values_are_clamped(value, expected) -> None:
    assert gui._as_float(value, 3.0) == expected


def test_each_run_mints_a_fresh_token(tmp_path: Path) -> None:
    first = gui.Backend(root=str(tmp_path))
    second = gui.Backend(root=str(tmp_path))
    assert first.token != second.token
    assert len(first.token) >= 24


# --------------------------------------------------------------------------- #
# regressions
# --------------------------------------------------------------------------- #


def test_a_non_ascii_token_is_refused_not_a_crash(served) -> None:
    """`secrets.compare_digest` raises TypeError on non-ASCII text; bytes do not."""
    _, base = served
    assert _get(f"{base}/api/sessions?days=7&t=%D1%82%D0%BE%D0%BA%D0%B5%D0%BD")[0] == 403


def test_an_oversized_body_does_not_poison_keep_alive(served) -> None:
    """An undrained body would be parsed as the next request on the same socket."""
    import http.client

    backend, base = served
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        payload = json.dumps({"ids": ["x" * (400 * 1024)], "days": 7}).encode()
        connection.request(
            "POST", f"/api/commands?t={backend.token}", body=payload,
            headers={"Content-Type": "application/json"},
        )
        assert connection.getresponse().read() is not None

        connection.request("GET", f"/api/sessions?days=7&t={backend.token}")
        second = connection.getresponse()
        assert second.status == 200, "the connection was left mid-body"
        json.loads(second.read())
    finally:
        connection.close()


def test_a_bad_content_length_is_survivable(served) -> None:
    import http.client

    backend, base = served
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.putrequest("POST", f"/api/commands?t={backend.token}")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        connection.send(b"{}")
        assert connection.getresponse().status == 200
    finally:
        connection.close()


def test_the_heartbeat_keeps_the_backend_alive(served) -> None:
    backend, base = served
    backend.last_seen -= 1000
    assert _get(f"{base}/api/ping?t={backend.token}")[0] == 200
    assert backend.last_seen > 0

    import time

    assert time.monotonic() - backend.last_seen < 5


def test_wait_until_idle_returns_when_the_ui_goes_quiet(tmp_path: Path) -> None:
    import time

    backend = gui.Backend(root=str(tmp_path))
    backend.last_seen = time.monotonic() - 100
    started = time.monotonic()
    backend.wait_until_idle(grace=0.0)
    assert time.monotonic() - started < 6, "should give up quickly once nothing reports in"


def test_repeat_scans_are_served_from_cache(served, monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI fires several requests per click; each must not re-walk the config."""
    backend, base = served
    calls = {"n": 0}
    original = revenant.scan_sessions

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(revenant, "scan_sessions", counted)
    for _ in range(3):
        _get(f"{base}/api/sessions?days=7&t={backend.token}")
    _post(f"{base}/api/commands?t={backend.token}", {"ids": [], "days": 7})
    assert calls["n"] == 1


def test_a_different_window_bypasses_the_cache(served, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, base = served
    calls = {"n": 0}
    original = revenant.scan_sessions

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(revenant, "scan_sessions", counted)
    _get(f"{base}/api/sessions?days=7&t={backend.token}")
    _get(f"{base}/api/sessions?days=30&t={backend.token}")
    assert calls["n"] == 2


def test_the_ui_file_is_findable(tmp_path: Path) -> None:
    assert (gui.UI_DIR / "index.html").is_file(), "run_gui refuses to start without it"


# --------------------------------------------------------------------------- #
# a revival's loose ends
# --------------------------------------------------------------------------- #
ALPHA = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def no_launch(monkeypatch: pytest.MonkeyPatch) -> list:
    """Stand in for the launcher: record what would open, open nothing."""
    calls: list = []

    def fake(sessions, **kwargs):
        calls.append([s.session_id for s in sessions])
        if kwargs.get("landed") is not None:
            kwargs["landed"].extend(sessions)
        return 0

    monkeypatch.setattr(revenant, "launch", fake)
    return calls


def test_nan_and_infinite_days_fall_back(served) -> None:
    """NaN slips through min and max and then fails inside timedelta."""
    assert gui._as_float("nan", 3.0) == 3.0
    assert gui._as_float("inf", 3.0) == 3.0
    backend, base = served
    status, body = _get(f"{base}/api/sessions?days=nan&t={backend.token}")
    assert status == 200 and json.loads(body)["error"] is None


def test_a_body_that_is_not_an_object_gets_an_answer(served, no_launch) -> None:
    backend, base = served
    request = urllib.request.Request(
        f"{base}/api/revive?t={backend.token}", data=b"[1, 2, 3]",
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["ok"] is False and no_launch == []
    status, body = _post(f"{base}/api/revive?t={backend.token}", {"ids": "not-a-list", "days": 7})
    assert status == 200 and json.loads(body)["ok"] is False


def test_a_route_that_throws_still_answers(served, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, base = served

    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(backend, "sessions", broken)
    status, body = _get(f"{base}/api/sessions?days=7&t={backend.token}")
    assert status == 200
    assert "disk on fire" in json.loads(body)["error"]


def test_revive_reports_exactly_which_sessions_it_raised(served, no_launch) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/revive?t={backend.token}", {"ids": [ALPHA], "days": 7})
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["raised"] == [ALPHA] and payload["count"] == 1
    assert no_launch == [[ALPHA]]


def test_a_session_resumed_since_the_list_loaded_is_held_back(served, no_launch, monkeypatch) -> None:
    """The list can be seconds old; liveness is read again at launch time."""
    backend, base = served
    _get(f"{base}/api/sessions?days=7&t={backend.token}")  # warm the cache while it is dead
    monkeypatch.setattr(revenant, "load_live_registry", lambda root, agent: {ALPHA: {"pid": 4242}})
    status, body = _post(f"{base}/api/revive?t={backend.token}", {"ids": [ALPHA], "days": 7})
    payload = json.loads(body)
    assert payload["ok"] is False and payload["raised"] == []
    assert "still running" in payload["message"]
    assert no_launch == []


def test_more_than_the_cap_says_what_was_left(served, no_launch) -> None:
    backend, base = served
    ids = [ALPHA] + [f"missing-{i}" for i in range(gui.MAX_IDS + 5)]
    status, body = _post(f"{base}/api/revive?t={backend.token}", {"ids": ids, "days": 7})
    payload = json.loads(body)
    assert f"Only the first {gui.MAX_IDS} were sent" in payload["message"]
    assert payload["raised"] == [ALPHA]


def test_the_page_learns_what_each_layout_really_opens(served) -> None:
    backend, base = served
    status, body = _get(f"{base}/api/sessions?days=7&t={backend.token}")
    layouts = json.loads(body)["layouts"]
    assert set(layouts) == {"tabs", "windows"}
    for wanted, info in layouts.items():
        assert info["opens"] in {"tabs", "windows"}
        assert info["terminal"]
        assert (info["note"] == "") == (info["opens"] == wanted)
