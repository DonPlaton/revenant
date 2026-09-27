"""Dealing into the real window: where it goes, how big it comes out, and when.

Nothing here starts a terminal. The argv is read, not run; the one schedule
test runs Python itself, and the routes are exercised with the launcher stood in.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revenant  # noqa: E402
import revenant_gui as gui  # noqa: E402
import revenant_terminals as terminals  # noqa: E402

from test_gui import ALPHA, _post, _transcript, served  # noqa: E402,F401

SCREEN = (0, 0, 1920, 1020)


def _cells(scale: float = 1.25, **settings) -> terminals.Cells:
    cells = terminals.wt_cells(settings, scale)
    assert cells is not None
    return cells


# --------------------------------------------------------------------------- #
# reading Windows Terminal's settings
# --------------------------------------------------------------------------- #
def test_settings_with_comments_and_trailing_commas_are_read() -> None:
    text = """
    // The file Windows Terminal writes on first run
    {
        "defaultProfile": "{abc}", /* a note */
        "profiles": {
            "defaults": { "font": { "size": 11, }, },
            "list": [ { "guid": "{abc}", "commandline": "cmd /c echo // not a comment, ]" }, ],
        },
    }
    """
    data = terminals._jsonc(text)
    assert data["profiles"]["defaults"]["font"]["size"] == 11
    assert data["profiles"]["list"][0]["commandline"] == "cmd /c echo // not a comment, ]"


def test_a_comma_before_a_commented_out_entry_is_dropped() -> None:
    """The last entry is the one people comment out, and Terminal reads that fine."""
    text = """{ "actions": [
        { "command": "find", "keys": "ctrl+shift+f" },
        // { "command": "paste", "keys": "ctrl+v" }
    ], "b": { "c": 1, /* gone */ }, }"""
    assert terminals._jsonc(text) == {"actions": [{"command": "find", "keys": "ctrl+shift+f"}], "b": {"c": 1}}


def test_the_cell_is_rounded_the_way_windows_terminal_rounds_it() -> None:
    """Cascadia's "0" is 1200/2048 em and ascent plus descent 2380/2048 em, each
    rounded to the nearest pixel: at 100% an unrounded 18.4 px row made 30 rows
    come out 18 px taller than the room they were fitted into."""
    one = _cells(1.0)
    assert (one.width, one.height) == (9, 19)
    half = _cells(1.5)
    assert (half.width, half.height) == (14, 28)


@pytest.mark.parametrize("face, placed", [
    ("Cascadia Mono", True),
    ("Cascadia Code NF", True),
    ("cascadia mono, Consolas", True),
    ("JetBrains Mono", False),
    ("Consolas, Cascadia Mono", False),
])
def test_only_cascadias_cells_are_predicted(face: str, placed: bool) -> None:
    """Another face has its own metrics, and a guess at them opens a window
    larger than the room. The face set on the profile is the one that counts."""
    modern = {"profiles": {"defaults": {"font": {"face": face}}}}
    legacy = {"profiles": {"defaults": {"fontFace": face}}}
    assert (terminals.wt_cells(modern, 1.25) is not None) is placed
    assert (terminals.wt_cells(legacy, 1.25) is not None) is placed
    shadowed = {"defaultProfile": "{a}", "profiles": {"defaults": {"font": {"face": "Cascadia Mono"}},
                                                     "list": [{"guid": "{a}", "font": {"face": face}}]}}
    assert (terminals.wt_cells(shadowed, 1.25) is not None) is placed


def test_numbers_that_do_not_read_fall_back_to_the_defaults() -> None:
    """Parsed from JSON, 1e999 is infinity and "nan" a float: neither may reach int()."""
    odd = terminals.wt_cells({"initialCols": 1e999, "initialRows": float("nan"),
                              "profiles": {"defaults": {"padding": "nan"}}}, 1.0)
    assert odd is not None and (odd.cols, odd.rows) == (120, 30)
    assert odd.chrome_w == _cells(1.0).chrome_w


def test_the_default_cell_is_what_was_measured() -> None:
    """Cascadia Mono 12 pt at 125%: 12 px wide, about 23 px tall, measured on a
    real window at 100 x 30 and 90 x 26 cells."""
    cells = _cells()
    assert cells.width == 12
    assert 22.5 <= cells.height <= 23.5
    width, height = cells.size(100, 30)
    assert abs(width - 1241) <= 12 and abs(height - 761) <= 12


@pytest.mark.parametrize("settings", [
    {"launchMode": "maximized"},
    {"launchMode": "fullscreen"},
    {"launchMode": "focus"},
    {"launchMode": "maximizedFocus"},
    {"initialPosition": "100,100"},
    {"centerOnLaunch": True},
    {"showTabsInTitlebar": False},
    {"profiles": {"defaults": {"font": {"cellWidth": "1.2"}}}},
    {"profiles": {"defaults": {"font": {"size": "large"}}}},
])
def test_a_window_the_user_placed_or_shaped_is_left_alone(settings: dict) -> None:
    assert terminals.wt_cells(settings, 1.25) is None


def test_no_settings_or_an_odd_scale_places_nothing() -> None:
    assert terminals.wt_cells(None, 1.25) is None
    assert terminals.wt_cells({}, 0) is None
    assert terminals.wt_cells({}, float("nan")) is None


def test_the_default_profiles_font_wins_over_the_defaults() -> None:
    settings = {
        "defaultProfile": "{B}",
        "profiles": {
            "defaults": {"font": {"size": 10}},
            "list": [{"guid": "{A}", "font": {"size": 20}}, {"guid": "{b}", "fontSize": 14}],
        },
    }
    assert _cells(1.0, **settings).width == 11  # 14 pt at 96 dpi: 18.7 px, 1200/2048 of it is 10.9
    assert _cells(1.0, profiles={"defaults": {"font": {"size": 10}}}).width == 8


def test_padding_and_a_hidden_scrollbar_change_the_frame() -> None:
    plain = _cells(1.0)
    tight = _cells(1.0, profiles={"defaults": {"padding": "0", "scrollbarState": "hidden"}})
    assert plain.chrome_w - tight.chrome_w == pytest.approx(32)
    assert plain.chrome_h - tight.chrome_h == pytest.approx(16)
    assert terminals._padding("4, 2") == (4, 2, 4, 2)
    assert terminals._padding("1, 2, 3, 4") == (1, 2, 3, 4)
    assert terminals._padding("wide") == (8, 8, 8, 8)


# --------------------------------------------------------------------------- #
# placing the window
# --------------------------------------------------------------------------- #
def test_a_placed_window_is_whole_readable_and_on_screen() -> None:
    """Every scale from 100% to 200%, the app anywhere across the screen."""
    placed = 0
    for scale in (1.0, 1.25, 1.5, 2.0):
        cells = _cells(scale)
        screen = tuple(round(v * scale / 1.25) for v in SCREEN)
        for left in range(0, 1600, 100):
            for top in range(0, 800, 50):
                room = terminals.Room(screen, round(left * scale / 1.25), round(top * scale / 1.25),
                                      round((top + 300) * scale / 1.25))
                spot = terminals.place(room, cells)
                if spot is None:
                    continue
                placed += 1
                where = (scale, left, top)
                assert spot.x >= room.left and spot.y >= room.top, where
                assert spot.y <= room.bottom, f"{where}: the strip is where the cards land"
                assert spot.x + spot.width <= screen[2], f"{where}: caption buttons off screen"
                assert spot.y + spot.height <= screen[3], where
                assert spot.cols >= terminals.PLACE_MIN[0] and spot.rows >= terminals.PLACE_MIN[1], where
                assert spot.cols <= cells.cols and spot.rows <= cells.rows, where
    assert placed > 100, "most of the screen leaves room for a window"


def test_a_window_with_no_room_is_not_placed() -> None:
    cells = _cells()
    assert terminals.place(terminals.Room(SCREEN, 1000, 300, 600), cells) is None, "fewer than 80 columns"
    assert terminals.place(terminals.Room(SCREEN, 400, 600, 600), cells) is None, "fewer than 20 rows"
    assert terminals.place(terminals.Room(SCREEN, 400, 700, 600), cells) is None, "strip below the band"


def test_a_roomy_screen_gets_the_users_own_size() -> None:
    spot = terminals.place(terminals.Room((0, 0, 3840, 2100), 900, 300, 900), _cells())
    assert spot is not None and (spot.cols, spot.rows) == (120, 30)


def test_the_tabbed_call_is_told_where_to_open() -> None:
    spot = terminals.Spot(448, 358, 118, 24, 1458, 630)
    jobs = [terminals.Job(f"s{i}", rf"D:\p\{i}", f"claude --resume {i}") for i in range(3)]
    argv = terminals.WindowsTerminal().plan(jobs, layout="tabs", place=spot).commands[0]
    assert argv[1:7] == ["-w", "new", "--pos", "448,358", "--size", "118,24"]
    assert argv.index("--pos") < argv.index("new-tab")


def test_only_the_call_that_makes_the_window_places_it() -> None:
    """Split over several calls, the later ones add tabs to the named window."""
    spot = terminals.Spot(448, 358, 118, 24, 1458, 630)
    jobs = [terminals.Job(f"session {i} " + "x" * 60, f"D:\\projects\\{i:04d}-" + "y" * 40,
                          f"claude --resume {i:08d}-aaaa-bbbb-cccc-dddddddddddd") for i in range(400)]
    commands = terminals.WindowsTerminal().plan(jobs, layout="tabs", place=spot).commands
    assert len(commands) > 1
    assert "--pos" in commands[0] and not any("--pos" in argv for argv in commands[1:])
    import subprocess
    assert all(len(subprocess.list2cmdline(argv)) < 32_767 for argv in commands)


def test_windows_of_their_own_are_never_placed() -> None:
    spot = terminals.Spot(448, 358, 118, 24, 1458, 630)
    jobs = [terminals.Job(f"s{i}", rf"D:\p\{i}", f"claude --resume {i}") for i in range(3)]
    commands = terminals.WindowsTerminal().plan(jobs, layout="windows", place=spot).commands
    assert not any("--pos" in argv for argv in commands)


# --------------------------------------------------------------------------- #
# the schedule
# --------------------------------------------------------------------------- #
def test_a_plan_waits_for_its_moment() -> None:
    plan = terminals.Plan("test", [[sys.executable, "-c", "pass"]], delays=(0.3,))
    began = time.monotonic()
    opened, _ = terminals.run(plan)
    assert opened == 1
    assert time.monotonic() - began >= 0.3


def test_a_plan_counts_its_moment_from_when_it_was_asked() -> None:
    """Time spent checking the sessions before the run is not added on top."""
    asked = time.monotonic() - 1.0
    plan = terminals.Plan("test", [[sys.executable, "-c", "pass"]], delays=(0.3,), origin=asked)
    began = time.monotonic()
    opened, _ = terminals.run(plan)
    assert opened == 1
    assert time.monotonic() - began < terminals.EARLY_EXIT_GRACE + 0.25


def test_launch_holds_every_call_back_and_hands_the_place_on(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def plan_launch(sessions, **kwargs):
        seen["place"] = kwargs.get("place")
        return terminals.WindowsTerminal(), terminals.Plan("wt", [["a"], ["b"]], layout="tabs")

    def run(plan):
        seen["delays"] = plan.delays
        seen["origin"] = plan.origin
        return terminals.Ran(len(plan.commands), "", (True,) * len(plan.commands))

    monkeypatch.setattr(revenant, "plan_launch", plan_launch)
    monkeypatch.setattr(terminals, "run", run)
    session = revenant.Session(session_id="abc", transcript=Path("x.jsonl"), project_slug="s", cwd=Path("/tmp/x"))
    spot = terminals.Spot(1, 2, 80, 20, 1000, 500)
    assert revenant.launch([session], place=spot, start_in=0.5, stream=io.StringIO()) == 0
    assert seen["place"] is spot
    assert seen["delays"] == (0.5, 0.5)
    revenant.launch([session], start_in=0.5, stagger=0.1, stream=io.StringIO(), origin=123.0)
    assert seen["delays"] == pytest.approx((0.5, 0.6))
    assert seen["origin"] == 123.0


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
ROOM = {"scale": 1.25, "screen": [0, 0, 1536, 816], "left": 358.0, "top": 286.0, "bottom": 542.0}


@pytest.fixture
def windows_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows Terminal with its settings as shipped, on any platform."""
    monkeypatch.setattr(terminals, "choose", lambda *a, **k: terminals.WindowsTerminal())
    monkeypatch.setattr(terminals, "wt_settings", lambda: {})
    monkeypatch.setattr(terminals, "wt_can_place", lambda: True)
    monkeypatch.setattr(terminals, "display_scale", lambda x, y: None)


def test_the_page_is_told_where_the_window_will_open(served, windows_terminal) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/place?t={backend.token}", {"layout": "tabs", "room": ROOM})
    answer = json.loads(body)
    assert status == 200 and len(answer["spots"]) == 1
    spot = answer["spots"][0]
    assert spot["x"] == pytest.approx(358.4) and spot["y"] == pytest.approx(286.4)
    assert spot["x"] + spot["width"] <= 1536 and spot["y"] + spot["height"] <= 816
    assert answer["strip"] > 20


def test_placing_asks_for_the_token(served, windows_terminal) -> None:
    _, base = served
    status, _ = _post(f"{base}/api/place", {"layout": "tabs", "room": ROOM})
    assert status == 403


@pytest.mark.parametrize("payload", [
    {"layout": "windows", "room": ROOM},
    {"layout": "tabs", "room": {**ROOM, "left": 1400}},
    {"layout": "tabs", "room": {**ROOM, "scale": "big"}},
    {"layout": "tabs", "room": {**ROOM, "screen": [0, 0, 1536]}},
    {"layout": "tabs", "room": {**ROOM, "top": float("inf")}},
    {"layout": "tabs", "room": {**ROOM, "scale": 4, "screen": [0, 0, 1e308, 1e308]}},
    {"layout": "tabs", "room": {**ROOM, "scale": 40}},
    {"layout": "tabs", "room": "everywhere"},
    {"layout": "tabs"},
])
def test_no_place_is_an_empty_answer_not_an_error(served, windows_terminal, payload) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/place?t={backend.token}", payload)
    assert status == 200 and json.loads(body)["spots"] == []


def test_a_terminal_too_old_for_pos_is_not_placed(served, windows_terminal, monkeypatch) -> None:
    """Before 1.17 --pos is an error box and no tab, with the launcher exiting 0."""
    monkeypatch.setattr(terminals, "wt_can_place", lambda: False)
    backend, base = served
    _, body = _post(f"{base}/api/place?t={backend.token}", {"layout": "tabs", "room": ROOM})
    assert json.loads(body)["spots"] == []


def test_the_version_is_read_from_the_package_names() -> None:
    names = [
        "Microsoft.WindowsTerminal_1.16.10262.0_x64__8wekyb3d8bbwe",
        "Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe",
        "Microsoft.WindowsTerminalPreview_1.25.1.0_x64__8wekyb3d8bbwe",
        "Microsoft.WindowsTerminal_garbage_x64__8wekyb3d8bbwe",
        "Contoso.WindowsTerminal_9.0.0.0_x64__other",
    ]
    assert terminals._newest(names, "Microsoft.WindowsTerminal_8wekyb3d8bbwe") == (1, 24, 11911, 0)
    assert terminals._newest(names, "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe") == (1, 25, 1, 0)
    assert terminals._newest([], "Microsoft.WindowsTerminal_8wekyb3d8bbwe") is None


def test_a_zoomed_page_is_not_placed(served, windows_terminal, monkeypatch) -> None:
    """At 90% on a 125% display the page says 1.125, and cells worked out from
    that come out 10% smaller than the ones Terminal draws."""
    monkeypatch.setattr(terminals, "display_scale", lambda x, y: 1.25)
    backend, base = served
    _, body = _post(f"{base}/api/place?t={backend.token}", {"layout": "tabs", "room": {**ROOM, "scale": 1.125}})
    assert json.loads(body)["spots"] == []
    _, body = _post(f"{base}/api/place?t={backend.token}", {"layout": "tabs", "room": ROOM})
    assert len(json.loads(body)["spots"]) == 1


def test_only_windows_terminal_is_placed(served, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminals, "choose", lambda *a, **k: terminals.WindowsConsole())
    monkeypatch.setattr(terminals, "wt_settings", lambda: {})
    backend, base = served
    _, body = _post(f"{base}/api/place?t={backend.token}", {"layout": "tabs", "room": ROOM})
    assert json.loads(body)["spots"] == []


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list:
    """Stand in for the launcher and keep what it was asked."""
    calls: list = []

    def fake(sessions, **kwargs):
        calls.append(kwargs)
        kwargs["landed"].extend(sessions)
        return 0

    monkeypatch.setattr(revenant, "launch", fake)
    return calls


def test_the_revival_opens_where_and_when_the_page_drew_it(served, windows_terminal, launched) -> None:
    backend, base = served
    status, body = _post(f"{base}/api/revive?t={backend.token}",
                         {"ids": [ALPHA], "days": 7, "layout": "tabs", "room": ROOM, "wait": 800})
    assert json.loads(body)["raised"] == [ALPHA]
    (kwargs,) = launched
    assert isinstance(kwargs["place"], terminals.Spot) and kwargs["place"].x == 448
    assert 0.5 <= kwargs["start_in"] <= 0.8


def test_a_room_or_a_wait_that_does_not_read_still_launches(served, windows_terminal, launched) -> None:
    backend, base = served
    _post(f"{base}/api/revive?t={backend.token}",
          {"ids": [ALPHA], "days": 7, "layout": "tabs", "room": {"scale": "?"}, "wait": "soon"})
    _post(f"{base}/api/revive?t={backend.token}",
          {"ids": [ALPHA], "days": 7, "layout": "tabs", "wait": 1e9, "stagger": 1e9})
    first, second = launched
    assert first["place"] is None and first["start_in"] == 0 and first["stagger"] == 0
    assert second["start_in"] <= gui.MAX_WAIT_MS / 1000
    assert second["stagger"] == gui.MAX_STAGGER_MS / 1000


def test_a_room_that_breaks_the_placing_still_launches(served, windows_terminal, launched, monkeypatch) -> None:
    def broken(*_):
        raise OverflowError("cannot convert float infinity to integer")

    monkeypatch.setattr(terminals, "wt_cells", broken)
    backend, base = served
    _, body = _post(f"{base}/api/revive?t={backend.token}",
                    {"ids": [ALPHA], "days": 7, "layout": "tabs", "room": ROOM})
    assert json.loads(body)["raised"] == [ALPHA]
    assert launched[0]["place"] is None


def test_the_schedule_counts_from_the_request(served, launched) -> None:
    backend, base = served
    before = time.monotonic()
    _post(f"{base}/api/revive?t={backend.token}", {"ids": [ALPHA], "days": 7, "wait": 800})
    (kwargs,) = launched
    assert kwargs["start_in"] == pytest.approx(0.8)
    assert before <= kwargs["origin"] <= time.monotonic()


def test_windows_are_never_spread_past_the_pages_patience(served, launched, monkeypatch) -> None:
    """200 windows at the widest gap would take over a minute, past the moment
    the page gives up, re-enables REVIVE and invites a second launch."""
    backend, base = served
    others = [f"2222222{i}-2222-2222-2222-222222222222" for i in range(3)]
    for index, sid in enumerate(others):
        _transcript(backend.root, f"D--Coding-b{index}", sid, rf"D:\Coding\b{index}", age_hours=2)
    backend.invalidate()
    monkeypatch.setattr(gui, "MAX_SPREAD_MS", 300.0)
    _post(f"{base}/api/revive?t={backend.token}",
          {"ids": [ALPHA, *others], "days": 7, "layout": "windows", "stagger": 400})
    (kwargs,) = launched
    assert kwargs["stagger"] == pytest.approx(0.1)
    assert gui.MAX_WAIT_MS + gui.MAX_SPREAD_MS < 60_000


def test_closing_the_app_waits_for_a_revival_still_starting(served, monkeypatch) -> None:
    """The service's threads die with the process, and with them the rest of a
    schedule, so the app does not exit while a revival is still under way."""
    backend, _ = served
    going, release = threading.Event(), threading.Event()

    def slow(sessions, **kwargs):
        going.set()
        release.wait(5)
        kwargs["landed"].extend(sessions)
        return 0

    monkeypatch.setattr(revenant, "launch", slow)
    worker = threading.Thread(target=lambda: backend.revive([ALPHA], days=7))
    worker.start()
    assert going.wait(5)
    assert backend.settle(timeout=0.1) is False
    release.set()
    assert backend.settle(timeout=5) is True
    worker.join(5)


def test_every_layout_says_how_long_its_windows_take(served) -> None:
    backend, _ = served
    for choice in backend.layouts().values():
        assert choice["lead"] >= 0
