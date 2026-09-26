#!/usr/bin/env python3
"""Where a revived session actually opens.

Each terminal backend turns a list of jobs into argv lists. Nothing here runs a
command while building a plan, so every backend is covered by tests on all three
platforms even though only one of them can be exercised for real at a time.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: How long a spawned terminal gets to fail before it counts as opened. Long
#: enough to catch an immediate exit, short enough not to be felt.
EARLY_EXIT_GRACE = 1.0
#: How long a serial command gets to finish before the next one starts anyway.
#: Launchers hand off and return at once; a terminal that is its own window
#: never returns, and must not hold the rest up.
SERIAL_WAIT = 2.0
#: How much of Windows' 32,767-character command line one wt.exe call may use,
#: leaving room for the binary's own path and the window argument.
WT_COMMAND_LINE = 30_000

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"

#: Where the revived sessions land. Every backend can do `windows`; the ones that
#: can script a tab say so, and the rest degrade to windows with a note rather
#: than refusing to open anything.
LAYOUT_TABS = "tabs"
LAYOUT_WINDOWS = "windows"
LAYOUTS = (LAYOUT_TABS, LAYOUT_WINDOWS)
DEFAULT_LAYOUT = LAYOUT_TABS


@dataclass(frozen=True)
class Job:
    """One session to bring back: a name, a directory, and the command to run."""

    label: str
    cwd: str
    command: str


@dataclass(frozen=True)
class Plan:
    """Everything needed to open a set of jobs, without having opened anything yet."""

    terminal: str
    commands: list[list[str]] = field(default_factory=list)
    note: str = ""
    #: Directory to start each command in, positionally. Empty means "wherever".
    cwds: list[str] = field(default_factory=list)
    #: Whether each command needs a console window of its own (Windows only).
    new_console: bool = False
    #: Commands whose failure is expected and must not count against the run.
    optional: frozenset[int] = frozenset()
    #: Commands that set the stage rather than open a window, so counting them
    #: would report more terminals than the user can see.
    overhead: frozenset[int] = frozenset()
    #: Commands that must run one after another: each needs what the one before
    #: it opened, like a tab that goes into the window the last call made.
    serial: bool = False
    #: Seconds to wait after each serial command. A launcher that hands off and
    #: exits at once has not necessarily brought its window up yet.
    pause: float = 0.0
    #: How many jobs each command opens, in order, when that is neither one per
    #: command nor all in one. Commands in `overhead` open none.
    carries: tuple[int, ...] = ()
    #: Seconds after the run starts before each command may go, so windows can
    #: come up on a schedule: as the cards that stand for them land.
    delays: tuple[float, ...] = ()
    #: What this plan actually does, which is not always what was asked for: a
    #: backend that cannot script tabs reports `windows` here and says so in the
    #: note, so the caller never claims to have opened tabs that do not exist.
    layout: str = LAYOUT_WINDOWS

    def directory(self, index: int) -> str:
        return self.cwds[index] if index < len(self.cwds) else ""

    def shares(self, jobs: int) -> list[int]:
        """How many of `jobs` each command opens: one each, or all in one."""
        if self.carries:
            return list(self.carries)
        real = [i for i in range(len(self.commands)) if i not in self.overhead]
        each = jobs if len(real) == 1 else 1
        return [0 if i in self.overhead else each for i in range(len(self.commands))]

    def render(self) -> str:
        lines = []
        for index, argv in enumerate(self.commands):
            line = shlex.join(argv)
            if index in self.optional:
                line += " || true"
            cwd = self.directory(index)
            lines.append(f"cd {shlex.quote(cwd)} && {line}" if cwd else line)
        return "\n".join(lines)


def _posix_payload(job: Job) -> str:
    """`cd` into the directory, run the agent, then leave a shell behind.

    The trailing `exec` mirrors PowerShell's `-NoExit`: when the agent exits you
    keep the terminal and its scrollback instead of the window vanishing.
    """
    shell = os.environ.get("SHELL") or "/bin/sh"
    return f"cd {shlex.quote(job.cwd)} && {job.command}; exec {shlex.quote(shell)} -i"


def wt_text(value: str) -> str:
    """A value wt.exe will not split. It reads every `;` as a break between
    commands, quoted or not, and `\\;` as a literal one."""
    return value.replace(";", "\\;")


def budget_groups(costs: list[int], budget: int) -> list[list[int]]:
    """Split items into runs whose summed cost stays within `budget`, in order."""
    groups: list[list[int]] = [[]]
    size = 0
    for index, cost in enumerate(costs):
        if groups[-1] and size + cost > budget:
            groups.append([])
            size = 0
        groups[-1].append(index)
        size += cost
    return groups


def wt_tab_cost(label: str, cwd: str, command: str) -> int:
    """Characters one tab adds to a wt.exe command line, separator included."""
    return len(subprocess.list2cmdline(["new-tab", "--title", label, "-d", cwd,
                                        "powershell", "-NoExit", "-Command", command])) + 3


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell() -> str:
    return "pwsh" if shutil.which("pwsh") else "powershell"


#: Store package identities for Windows Terminal, stable first.
WT_PACKAGES = (
    "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
    "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe",
)


def windows_terminal_binary() -> str | None:
    """The wt.exe that can actually be started, or None.

    `wt.exe` sitting directly in WindowsApps is an app-execution alias, and
    starting it fails with ERROR_CANT_ACCESS_FILE (1920) on machines where the
    alias is switched off or its reparse point does not resolve for the calling
    process, all while Windows Terminal is installed and works perfectly by hand.
    The package's own folder beside it holds a second entry point with neither
    problem, so that one is tried first and the alias is only the fallback.
    """
    if not WINDOWS:
        return None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        apps = Path(local) / "Microsoft" / "WindowsApps"
        for package in WT_PACKAGES:
            candidate = apps / package / "wt.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which("wt.exe") or shutil.which("wt")


# --------------------------------------------------------------------------- #
# Placing windows where the deal put them
# --------------------------------------------------------------------------- #

#: The fewest columns and rows worth placing a window at. An agent's interface
#: wraps badly under 80 columns, and a window cut shorter than this is worse
#: than one wherever the terminal would have put it.
PLACE_MIN = (80, 20)


@dataclass(frozen=True)
class Cells:
    """How a Windows Terminal window's visible size follows from its columns and
    rows, in physical pixels at one display scale."""

    width: float
    height: float
    #: Padding, scrollbar and border beside the cells.
    chrome_w: float
    #: Tab row, padding and border above and below them.
    chrome_h: float
    #: The tab row alone, where the cards land.
    strip: float
    #: What a window opens with when no size is asked for.
    cols: int = 120
    rows: int = 30

    def size(self, cols: int, rows: int) -> tuple[int, int]:
        return round(cols * self.width + self.chrome_w), round(rows * self.height + self.chrome_h)

    def fit(self, width: float, height: float) -> tuple[int, int]:
        """The most columns and rows that stay inside `width` x `height`."""
        return int((width - self.chrome_w) // self.width), int((height - self.chrome_h) // self.height)


@dataclass(frozen=True)
class Spot:
    """Where one window goes, in physical screen pixels: the visible top-left
    corner that `--pos` sets, the `--size` asked for, and the visible size that
    comes out as far as the font lets it be predicted."""

    x: int
    y: int
    cols: int
    rows: int
    width: int
    height: int


@dataclass(frozen=True)
class Room:
    """The part of the screen the deal may put a window in, in physical pixels.

    `left` is where the window may start, clear of the figure. Its tab strip goes
    between `top` and `bottom`, where the cards can still be seen landing; the
    window itself may hang below that, but never past the work area in `screen`
    (left, top, right, bottom).
    """

    screen: tuple[int, int, int, int]
    left: int
    top: int
    bottom: int


def _jsonc(text: str):
    """Parse the JSON with comments and trailing commas that Windows Terminal
    writes and the json module refuses."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == ",":
            # A comma with nothing after it but a closing bracket is dropped.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if not (j < n and text[j] in "}]"):
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


def wt_settings() -> dict | None:
    """Windows Terminal's settings, from the package that `wt.exe` belongs to."""
    local = os.environ.get("LOCALAPPDATA")
    if not WINDOWS or not local:
        return None
    binary = windows_terminal_binary() or ""
    packages = sorted(WT_PACKAGES, key=lambda package: package not in binary)
    candidates = [Path(local) / "Packages" / package / "LocalState" / "settings.json" for package in packages]
    candidates.append(Path(local) / "Microsoft" / "Windows Terminal" / "settings.json")
    for path in candidates:
        try:
            data = _jsonc(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _padding(value: object) -> tuple[float, float, float, float]:
    """Windows Terminal's padding: one number, two (sides, ends) or four."""
    try:
        parts = [float(p) for p in str(value).split(",")]
    except ValueError:
        parts = [8.0]
    if len(parts) == 1:
        parts *= 4
    elif len(parts) == 2:
        parts = [parts[0], parts[1], parts[0], parts[1]]
    elif len(parts) != 4:
        parts = [8.0] * 4
    return parts[0], parts[1], parts[2], parts[3]


def wt_cells(settings: dict | None, scale: float) -> Cells | None:
    """How big a Windows Terminal window comes out at `scale`, or None.

    None when the user has said where windows go (a launch mode, a position,
    centring) or set something the estimate cannot follow. The cell is worked
    out from the font size the way Cascadia Mono measures, rounded up, so a
    window never comes out larger than the room it was given.
    """
    if not isinstance(settings, dict) or not 0.5 <= scale <= 4:
        return None
    if str(settings.get("launchMode", "default")) not in ("default", "focus"):
        return None
    if settings.get("initialPosition") or settings.get("centerOnLaunch"):
        return None
    if settings.get("showTabsInTitlebar", True) is False:
        return None
    profiles = settings.get("profiles")
    defaults: dict = {}
    listed: list = []
    if isinstance(profiles, dict):
        defaults = profiles.get("defaults") if isinstance(profiles.get("defaults"), dict) else {}
        listed = profiles.get("list") if isinstance(profiles.get("list"), list) else []
    elif isinstance(profiles, list):
        listed = profiles
    wanted = str(settings.get("defaultProfile", "")).lower()
    chosen = next((p for p in listed if isinstance(p, dict) and str(p.get("guid", "")).lower() == wanted), {})

    size = None
    for source in (chosen, defaults):
        font = source.get("font") if isinstance(source.get("font"), dict) else {}
        if "cellWidth" in font or "cellHeight" in font:
            return None
        if size is None:
            size = font.get("size", source.get("fontSize"))
    try:
        points = float(size) if size is not None else 12.0
    except (TypeError, ValueError):
        return None
    if not 4 <= points <= 72:
        return None

    def pick(key: str, fallback):
        for source in (chosen, defaults):
            if key in source:
                return source[key]
        return fallback

    left, top, right, bottom = _padding(pick("padding", "8"))
    scrollbar = 0.0 if str(pick("scrollbarState", "visible")) == "hidden" else 16.0
    pixels = points * 96 / 72 * scale
    try:
        cols = int(settings.get("initialCols", 120))
        rows = int(settings.get("initialRows", 30))
    except (TypeError, ValueError):
        cols, rows = 120, 30
    return Cells(
        # Rounded up, but not past a whole pixel by a float's last digit.
        width=math.ceil(0.6 * pixels - 1e-6),
        height=1.15 * pixels,
        chrome_w=(left + right + scrollbar + 2) * scale,
        chrome_h=(top + bottom + 46) * scale,
        strip=36 * scale,
        cols=max(PLACE_MIN[0], cols),
        rows=max(PLACE_MIN[1], rows),
    )


def place(room: Room, cells: Cells, *, margin: int = 12) -> Spot | None:
    """Where the one window of a tabbed revival goes, or None when it does not fit.

    It starts at `room.left` with its tab strip at the top of the band, and gets
    the columns and rows that keep it inside the work area, capped at what the
    user opens by default. Nothing is placed below `PLACE_MIN`.

    Only the tabbed window is placed. Filmed on a 1080p screen, windows of their
    own fit three to a cascade beside the figure, so ten of them landed in three
    piles with seven hidden, and Windows Terminal brought them up in a burst and
    out of order; where it cascades them itself, every one stays in view.
    """
    s_left, s_top, s_right, s_bottom = room.screen
    x, y = max(room.left, s_left + margin), max(room.top, s_top + margin)
    if y > room.bottom:
        return None
    cols, rows = cells.fit(s_right - margin - x, s_bottom - margin - y)
    cols, rows = min(cols, cells.cols), min(rows, cells.rows)
    if cols < PLACE_MIN[0] or rows < PLACE_MIN[1]:
        return None
    width, height = cells.size(cols, rows)
    return Spot(round(x), round(y), cols, rows, width, height)


#: How a backend describes what it did when it could not do what was asked.
_INSTEAD = {
    LAYOUT_WINDOWS: "opened one window per session",
    LAYOUT_TABS: "opened one tab per session",
}


class Terminal:
    """A way of opening terminals. Subclasses build the argv."""

    key = "terminal"
    label = "Terminal"
    #: Platforms this backend can run on: any of "nt", "darwin", "linux".
    platforms: tuple[str, ...] = ()
    #: Layouts this backend can actually produce. A window per session is the one
    #: thing every terminal in existence can do, so it is the floor.
    layouts: tuple[str, ...] = (LAYOUT_WINDOWS,)

    @property
    def tabs(self) -> bool:
        """True when this backend can put every session in one window."""
        return LAYOUT_TABS in self.layouts

    def settle(self, layout: str | None) -> str:
        """The layout this backend will really use for the one that was asked for."""
        wanted = layout or DEFAULT_LAYOUT
        return wanted if wanted in self.layouts else self.layouts[0]

    def demoted(self, layout: str | None) -> str:
        """The note to show when the asked-for layout is not on offer here."""
        wanted = layout or DEFAULT_LAYOUT
        if wanted in self.layouts:
            return ""
        return f"{self.label} cannot open {wanted}; {_INSTEAD[self.layouts[0]]}."

    def supported(self) -> bool:
        here = "nt" if WINDOWS else ("darwin" if MACOS else "linux")
        return here in self.platforms

    def available(self) -> bool:
        return self.supported()

    def plan(self, jobs: list[Job], **_) -> Plan:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


class WindowsTerminal(Terminal):
    key = "wt"
    label = "Windows Terminal"
    platforms = ("nt",)
    layouts = (LAYOUT_TABS, LAYOUT_WINDOWS)

    def available(self) -> bool:
        return self.supported() and bool(windows_terminal_binary())

    def plan(
        self,
        jobs: list[Job],
        *,
        layout: str | None = None,
        window: str = "new",
        profile: str | None = None,
        place: Spot | None = None,
        **_,
    ) -> Plan:
        shell = _powershell()
        binary = windows_terminal_binary() or "wt.exe"

        def tab(job: Job) -> list[str]:
            # A folder or a session name with a `;` in it would otherwise cut
            # the tab in two.
            argv = ["new-tab", "--title", wt_text(job.label), "-d", wt_text(job.cwd)]
            if profile:
                argv += ["-p", profile]
            return argv + [shell, "-NoExit", "-Command", wt_text(job.command)]

        if self.settle(layout) == LAYOUT_WINDOWS:
            # `-w new` is what makes each call a window of its own, so it overrides
            # the target window here: honouring `--window 0` would quietly merge
            # them back into tabs, which is the layout the caller just declined.
            return Plan(
                self.key,
                [[binary, "-w", "new", *tab(job)] for job in jobs],
                layout=LAYOUT_WINDOWS,
            )

        # A Windows command line holds 32,767 characters, which a couple of
        # hundred tabs pass. Past that the tabs go in several calls to one named
        # window, run in turn so each finds the window the first one made.
        pieces = [tab(job) for job in jobs]
        groups = budget_groups([len(subprocess.list2cmdline(p)) + 3 for p in pieces], WT_COMMAND_LINE)
        target = window
        if len(groups) > 1 and window == "new":
            target = f"revenant-{secrets.token_hex(4)}"
        # Only the call that makes the window can say where it goes.
        at = ["--pos", f"{place.x},{place.y}", "--size", f"{place.cols},{place.rows}"] if place else []
        commands = []
        for number, group in enumerate(groups):
            argv: list[str] = [binary, "-w", target, *(at if number == 0 else [])]
            for index, job in enumerate(group):
                if index:
                    argv.append(";")
                argv += pieces[job]
            commands.append(argv)
        split = len(commands) > 1
        return Plan(self.key, commands, layout=LAYOUT_TABS, serial=split, pause=1.0 if split else 0.0,
                    carries=tuple(len(group) for group in groups))


class WindowsConsole(Terminal):
    """One console window per session.

    `wt.exe` is a Store app-execution alias inside an ACL-locked folder and some
    shells are denied execution of it, so this always-works path stays.
    """

    key = "conhost"
    label = "Windows console"
    platforms = ("nt",)

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        """Spawn the shell directly, each in a console of its own.

        Going through `cmd /c start` sent the directory as one token of a command
        line that cmd re-parses, so a path holding `&`, `|` or `^` was cut in half
        and the window opened somewhere else entirely. Windows takes a working
        directory and a new-console flag at spawn time, where no punctuation in a
        path can reach them.
        """
        shell = _powershell()
        return Plan(
            self.key,
            [[shell, "-NoExit", "-Command", job.command] for job in jobs],
            self.demoted(layout),
            cwds=[job.cwd for job in jobs],
            new_console=True,
            layout=LAYOUT_WINDOWS,
        )


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #


class ITerm2(Terminal):
    key = "iterm2"
    label = "iTerm2"
    platforms = ("darwin",)
    layouts = (LAYOUT_TABS, LAYOUT_WINDOWS)

    def available(self) -> bool:
        return self.supported() and any(
            Path(p).exists()
            for p in ("/Applications/iTerm.app", Path.home() / "Applications/iTerm.app")
        )

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        chosen = self.settle(layout)
        lines = ['tell application "iTerm2"', "  activate"]
        if chosen == LAYOUT_WINDOWS:
            for job in jobs:
                payload = _applescript_string(_posix_payload(job))
                lines.append("  set w to (create window with default profile)")
                lines.append(f"  tell current session of w to write text {payload}")
        else:
            lines.append("  set w to (create window with default profile)")
            for index, job in enumerate(jobs):
                payload = _applescript_string(_posix_payload(job))
                if index == 0:
                    lines.append(f"  tell current session of w to write text {payload}")
                else:
                    lines.append("  tell w")
                    lines.append("    set t to (create tab with default profile)")
                    lines.append(f"    tell current session of t to write text {payload}")
                    lines.append("  end tell")
        lines.append("end tell")
        return Plan(self.key, [["osascript", "-e", "\n".join(lines)]], layout=chosen)


class MacTerminal(Terminal):
    """One window per session.

    Terminal.app exposes no way to make a tab from AppleScript; the usual trick is
    to have System Events press Command-T, which asks the user for accessibility
    permission and then types into whatever is frontmost. Not worth it: anyone who
    wants tabs on macOS has iTerm2, which scripts them properly.
    """

    key = "terminal-app"
    label = "Terminal.app"
    platforms = ("darwin",)

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        lines = ['tell application "Terminal"', "  activate"]
        lines += [f"  do script {_applescript_string(_posix_payload(job))}" for job in jobs]
        lines.append("end tell")
        return Plan(
            self.key,
            [["osascript", "-e", "\n".join(lines)]],
            self.demoted(layout),
            layout=LAYOUT_WINDOWS,
        )


# --------------------------------------------------------------------------- #
# Linux and cross-platform
# --------------------------------------------------------------------------- #


class GnomeTerminal(Terminal):
    key = "gnome-terminal"
    label = "GNOME Terminal"
    platforms = ("linux",)
    layouts = (LAYOUT_TABS, LAYOUT_WINDOWS)

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("gnome-terminal"))

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        """One call per tab, or per window.

        `--` ends option parsing for the whole command line, so a single call can
        carry only one command and the remaining tabs would open empty. Calling
        gnome-terminal once per job is the documented way: `--tab` opens a tab in
        the last-opened window, so they still land together, and `--window` is the
        same call with each one on its own instead.
        """
        chosen = self.settle(layout)
        where = "--tab" if chosen == LAYOUT_TABS else "--window"
        # Started all at once, the first --tab may not have a window to join yet
        # when the others arrive, and they scatter over several.
        return Plan(
            self.key,
            [
                [
                    "gnome-terminal",
                    where,
                    f"--title={job.label}",
                    f"--working-directory={job.cwd}",
                    "--",
                    "sh",
                    "-c",
                    _posix_payload(job),
                ]
                for job in jobs
            ],
            layout=chosen,
            serial=chosen == LAYOUT_TABS,
        )


class Konsole(Terminal):
    key = "konsole"
    label = "Konsole"
    platforms = ("linux",)
    layouts = (LAYOUT_TABS, LAYOUT_WINDOWS)

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("konsole"))

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        chosen = self.settle(layout)
        # Without --new-tab konsole opens a window, which is exactly the other half.
        where = ["--new-tab"] if chosen == LAYOUT_TABS else []
        # The first konsole started is the window the rest attach to, so they
        # have to wait for it rather than each opening a window of its own.
        return Plan(
            self.key,
            [
                ["konsole", *where, "--workdir", job.cwd, "-e", "sh", "-c", _posix_payload(job)]
                for job in jobs
            ],
            layout=chosen,
            serial=chosen == LAYOUT_TABS,
        )


class XfceTerminal(Terminal):
    key = "xfce4-terminal"
    label = "Xfce Terminal"
    platforms = ("linux",)
    layouts = (LAYOUT_TABS, LAYOUT_WINDOWS)

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("xfce4-terminal"))

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        chosen = self.settle(layout)
        if chosen == LAYOUT_WINDOWS:
            return Plan(
                self.key,
                [
                    [
                        "xfce4-terminal",
                        f"--title={job.label}",
                        f"--working-directory={job.cwd}",
                        f"--command=sh -c {shlex.quote(_posix_payload(job))}",
                    ]
                    for job in jobs
                ],
                layout=chosen,
            )

        argv = ["xfce4-terminal"]
        for job in jobs:
            argv += [
                "--tab",
                f"--title={job.label}",
                f"--working-directory={job.cwd}",
                f"--command=sh -c {shlex.quote(_posix_payload(job))}",
            ]
        return Plan(self.key, [argv], layout=chosen)


class _SimpleUnixTerminal(Terminal):
    """A terminal that takes a working directory and a command, one window each."""

    binary = ""
    directory_flag: tuple[str, ...] = ()
    command_flag: tuple[str, ...] = ()
    platforms = ("linux", "darwin")

    def available(self) -> bool:
        return self.supported() and bool(shutil.which(self.binary))

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        commands = []
        for job in jobs:
            argv = [self.binary, *self.directory_flag, job.cwd, *self.command_flag]
            commands.append(argv + ["sh", "-c", _posix_payload(job)])
        return Plan(self.key, commands, self.demoted(layout), layout=LAYOUT_WINDOWS)


class Kitty(_SimpleUnixTerminal):
    key = "kitty"
    label = "kitty"
    binary = "kitty"
    directory_flag = ("--directory",)


class WezTerm(_SimpleUnixTerminal):
    key = "wezterm"
    label = "WezTerm"
    binary = "wezterm"
    directory_flag = ("start", "--cwd")
    command_flag = ("--",)


class Alacritty(_SimpleUnixTerminal):
    key = "alacritty"
    label = "Alacritty"
    binary = "alacritty"
    directory_flag = ("--working-directory",)
    command_flag = ("-e",)


class Ghostty(_SimpleUnixTerminal):
    key = "ghostty"
    label = "Ghostty"
    binary = "ghostty"

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        return Plan(
            self.key,
            [
                [
                    "ghostty",
                    f"--working-directory={job.cwd}",
                    # -e consumes everything after it as the command, so it goes last.
                    "-e",
                    "sh",
                    "-c",
                    _posix_payload(job),
                ]
                for job in jobs
            ],
            self.demoted(layout),
            layout=LAYOUT_WINDOWS,
        )


class Foot(_SimpleUnixTerminal):
    key = "foot"
    label = "foot"
    binary = "foot"
    directory_flag = ("--working-directory",)
    platforms = ("linux",)


class Xterm(Terminal):
    key = "xterm"
    label = "xterm"
    platforms = ("linux",)

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("xterm"))

    def plan(self, jobs: list[Job], *, layout: str | None = None, **_) -> Plan:
        return Plan(
            self.key,
            [["xterm", "-T", job.label, "-e", "sh", "-c", _posix_payload(job)] for job in jobs],
            self.demoted(layout),
            layout=LAYOUT_WINDOWS,
        )


class Tmux(Terminal):
    """One tmux window per session.

    The natural home for anyone reviving sessions over SSH, and the only backend
    that survives the terminal emulator itself going away.
    """

    key = "tmux"
    label = "tmux"
    platforms = ("linux", "darwin", "nt")
    #: tmux windows are the tabs, and it has no windows of its own to open: the
    #: emulator hosting it owns those. Asking for windows here gets tabs and a note.
    layouts = (LAYOUT_TABS,)

    def available(self) -> bool:
        return bool(shutil.which("tmux"))

    #: Named so it can be killed again, and so an existing session has nothing
    #: matching it. Only ever created by the ensure step below.
    BOOT_WINDOW = "revenant-boot"

    def plan(
        self, jobs: list[Job], *, layout: str | None = None, session: str = "revenant", **_
    ) -> Plan:
        """Make sure the session exists, then add one window per job.

        Creating the session with the first job in it looked tidier, but
        `new-session` fails outright when the name is taken, and that failure cost
        exactly one session. `-A` makes the first step a no-op when the session is
        already there, which also means an emitted script can be run twice.
        """
        inside = bool(os.environ.get("TMUX"))
        commands: list[list[str]] = []
        optional: set[int] = set()
        overhead: set[int] = set()

        if not inside:
            commands.append(
                ["tmux", "new-session", "-A", "-d", "-s", session, "-n", self.BOOT_WINDOW]
            )
            overhead.add(0)

        target = [] if inside else ["-t", session]
        for job in jobs:
            commands.append(
                ["tmux", "new-window", *target, "-n", job.label, "-c", job.cwd, _posix_payload(job)]
            )

        if not inside:
            # Present only when this run created the session, so its absence is the
            # normal case and must not read as a failure.
            commands.append(["tmux", "kill-window", "-t", f"{session}:{self.BOOT_WINDOW}"])
            optional.add(len(commands) - 1)
            overhead.add(len(commands) - 1)

        note = "" if inside else f"Attach with: tmux attach -t {session}"
        aside = self.demoted(layout)
        note = f"{aside} {note}".strip() if aside else note
        return Plan(
            self.key,
            commands,
            note,
            optional=frozenset(optional),
            overhead=frozenset(overhead),
            layout=LAYOUT_TABS,
        )


#: Detection order per platform. The first available backend wins.
ORDER: dict[str, tuple[type[Terminal], ...]] = {
    "nt": (WindowsTerminal, WindowsConsole),
    "darwin": (ITerm2, MacTerminal),
    "linux": (GnomeTerminal, Konsole, XfceTerminal, Kitty, WezTerm, Ghostty, Alacritty, Foot, Xterm),
}

ALL: tuple[type[Terminal], ...] = (
    WindowsTerminal,
    WindowsConsole,
    ITerm2,
    MacTerminal,
    GnomeTerminal,
    Konsole,
    XfceTerminal,
    Kitty,
    WezTerm,
    Ghostty,
    Alacritty,
    Foot,
    Xterm,
    Tmux,
)
BY_KEY: dict[str, type[Terminal]] = {cls.key: cls for cls in ALL}


def here() -> str:
    return "nt" if WINDOWS else ("darwin" if MACOS else "linux")


def choose(preferred: str | None = None, *, layout: str | None = None) -> Terminal:
    """Pick a terminal: the requested one, tmux when we are already inside it, or the best available.

    With no name given, a backend that can produce the asked-for layout wins over
    one earlier in the order that cannot. Preference, not a requirement: if no
    installed terminal can do it, the usual first choice still opens the sessions
    and says in its note what it did instead.
    """
    if preferred:
        try:
            terminal = BY_KEY[preferred]()
        except KeyError:
            known = ", ".join(sorted(BY_KEY))
            raise SystemExit(f"Unknown terminal {preferred!r}. Known: {known}") from None
        return terminal

    # Inside tmux the emulator is not ours to drive, so tmux wins whatever the
    # layout: opening GUI windows from a session that may be on the far end of an
    # SSH connection puts them on the wrong machine.
    if os.environ.get("TMUX") and Tmux().available():
        return Tmux()

    installed = [t for t in (cls() for cls in ORDER.get(here(), ())) if t.available()]
    wanted = layout or DEFAULT_LAYOUT
    for terminal in installed:
        if wanted in terminal.layouts:
            return terminal
    if installed:
        return installed[0]
    if Tmux().available():
        return Tmux()
    return WindowsConsole() if WINDOWS else Xterm()


def fallbacks(after: Terminal, *, layout: str | None = None) -> list[Terminal]:
    """Backends worth trying when `after` refuses to start.

    Windows Terminal is the case that matters: it is a Store app-execution alias
    inside an ACL-locked folder, so it can be present, look available, and still
    fail with an access error the moment it is run.
    """
    here_now = ORDER.get(here(), ())
    seen = [t for t in (cls() for cls in here_now) if t.key != after.key and t.available()]

    wanted = layout or DEFAULT_LAYOUT
    # A terminal that can honour the layout goes first, but none is dropped: the
    # point of a fallback is that something opens. `sort` is stable, so the usual
    # order survives inside each group.
    seen.sort(key=lambda t: wanted not in t.layouts)

    # tmux stays last whatever the layout. It is the backend of last resort here:
    # on a desktop, dropping the sessions into a detached session nobody asked to
    # attach to looks exactly like nothing happening.
    if Tmux not in here_now:
        spare = Tmux()
        if spare.key != after.key and spare.available():
            seen.append(spare)
    return seen


def available_terminals() -> list[Terminal]:
    seen = [cls() for cls in ALL]
    return [terminal for terminal in seen if terminal.available()]


@dataclass(frozen=True)
class Outcome:
    """What running a plan put on screen."""

    #: Commands that brought something up, setup steps not counted.
    opened: int
    note: str
    #: Per command, whether it came up.
    landed: tuple[bool, ...]

    def jobs(self, plan: Plan, count: int) -> list[int]:
        """Indices of the jobs whose command came up."""
        found, start = [], 0
        for index, share in enumerate(plan.shares(count)):
            if index < len(self.landed) and self.landed[index]:
                found.extend(range(start, min(count, start + share)))
            start += share
        return found


def outcome(plan: Plan) -> Outcome:
    """Execute a plan and say, command by command, what came up.

    A terminal that spawns and then quits leaves nothing on screen, which is how
    Windows Terminal fails on a bad profile or an unreadable directory, so the
    started processes get a moment to fall over before any of them is counted.
    """
    # Each console gets its own window instead of fighting over the parent's.
    creation = 0x00000010 if (plan.new_console and WINDOWS) else 0

    landed = [False] * len(plan.commands)
    failures: list[str] = []
    started: list[tuple[int, subprocess.Popen]] = []
    begun = time.monotonic()
    for index, argv in enumerate(plan.commands):
        cwd = plan.directory(index) or None
        if index < len(plan.delays):
            wait = begun + plan.delays[index] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        try:
            if plan.terminal == "tmux":
                subprocess.run(argv, check=True, capture_output=True, timeout=30)
                landed[index] = True
            else:
                process = subprocess.Popen(argv, cwd=cwd, close_fds=True, creationflags=creation)
                started.append((index, process))
                if plan.serial:
                    try:
                        process.wait(timeout=SERIAL_WAIT)
                    except subprocess.TimeoutExpired:
                        pass  # still running is fine; the next one just goes
                    if plan.pause:
                        time.sleep(plan.pause)
        except (OSError, subprocess.SubprocessError) as exc:
            if index not in plan.optional:
                failures.append(str(exc))

    deadline = time.monotonic() + EARLY_EXIT_GRACE
    for index, process in started:
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            landed[index] = True  # still up, so it is a window on screen
            continue
        if code == 0:
            landed[index] = True  # a launcher that handed off and returned
        else:
            failures.append(f"{plan.terminal} exited with status {code}")

    opened = sum(1 for index, up in enumerate(landed) if up and index not in plan.overhead)
    note = "; ".join(failures[:3]) if failures else plan.note
    return Outcome(opened, note, tuple(landed))


class Ran(tuple):
    """(opened, message), which is what every caller unpacks, carrying which
    commands came up alongside for a caller that needs to know."""

    landed: tuple[bool, ...] = ()

    def __new__(cls, opened: int, note: str, landed: tuple[bool, ...] = ()):
        self = super().__new__(cls, (opened, note))
        self.landed = tuple(landed)
        return self


def arrived(plan: Plan, ran: tuple, count: int) -> list[int]:
    """Indices of the jobs whose command came up.

    A result without per-command detail, such as a stand-in, counts every job
    when anything opened and none when nothing did.
    """
    landed = getattr(ran, "landed", ())
    if not landed:
        return list(range(count)) if ran[0] else []
    return Outcome(ran[0], ran[1], landed).jobs(plan, count)


def run(plan: Plan) -> Ran:
    """Execute a plan. Returns (opened, message), with `.landed` per command.

    This is the one door every launch goes through, so a test that stands in
    for it stands in for every terminal.
    """
    result = outcome(plan)
    return Ran(result.opened, result.note, result.landed)
