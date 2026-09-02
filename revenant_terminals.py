#!/usr/bin/env python3
"""Where a revived session actually opens.

Each terminal backend turns a list of jobs into argv lists. Nothing here runs a
command while building a plan, so every backend is covered by tests on all three
platforms even though only one of them can be exercised for real at a time.
"""

from __future__ import annotations

import os
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

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"


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

    def directory(self, index: int) -> str:
        return self.cwds[index] if index < len(self.cwds) else ""

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


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell() -> str:
    return "pwsh" if shutil.which("pwsh") else "powershell"


class Terminal:
    """A way of opening terminals. Subclasses build the argv."""

    key = "terminal"
    label = "Terminal"
    #: Platforms this backend can run on: any of "nt", "darwin", "linux".
    platforms: tuple[str, ...] = ()
    #: True when every job lands in one window as tabs.
    tabs = False

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
    tabs = True

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("wt.exe") or shutil.which("wt"))

    def plan(self, jobs: list[Job], *, window: str = "new", profile: str | None = None, **_) -> Plan:
        argv: list[str] = ["wt.exe", "-w", window]
        shell = _powershell()
        for index, job in enumerate(jobs):
            if index:
                argv.append(";")
            argv += ["new-tab", "--title", job.label, "-d", job.cwd]
            if profile:
                argv += ["-p", profile]
            argv += [shell, "-NoExit", "-Command", job.command]
        return Plan(self.key, [argv])


class WindowsConsole(Terminal):
    """One console window per session.

    `wt.exe` is a Store app-execution alias inside an ACL-locked folder and some
    shells are denied execution of it, so this always-works path stays.
    """

    key = "conhost"
    label = "Windows console"
    platforms = ("nt",)

    def plan(self, jobs: list[Job], **_) -> Plan:
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
            cwds=[job.cwd for job in jobs],
            new_console=True,
        )


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #


class ITerm2(Terminal):
    key = "iterm2"
    label = "iTerm2"
    platforms = ("darwin",)
    tabs = True

    def available(self) -> bool:
        return self.supported() and any(
            Path(p).exists()
            for p in ("/Applications/iTerm.app", Path.home() / "Applications/iTerm.app")
        )

    def plan(self, jobs: list[Job], **_) -> Plan:
        lines = ['tell application "iTerm2"', "  activate", "  set w to (create window with default profile)"]
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
        return Plan(self.key, [["osascript", "-e", "\n".join(lines)]])


class MacTerminal(Terminal):
    key = "terminal-app"
    label = "Terminal.app"
    platforms = ("darwin",)

    def plan(self, jobs: list[Job], **_) -> Plan:
        lines = ['tell application "Terminal"', "  activate"]
        lines += [f"  do script {_applescript_string(_posix_payload(job))}" for job in jobs]
        lines.append("end tell")
        return Plan(self.key, [["osascript", "-e", "\n".join(lines)]])


# --------------------------------------------------------------------------- #
# Linux and cross-platform
# --------------------------------------------------------------------------- #


class GnomeTerminal(Terminal):
    key = "gnome-terminal"
    label = "GNOME Terminal"
    platforms = ("linux",)
    tabs = True

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("gnome-terminal"))

    def plan(self, jobs: list[Job], **_) -> Plan:
        """One call per tab.

        `--` ends option parsing for the whole command line, so a single call can
        carry only one command and the remaining tabs would open empty. Calling
        gnome-terminal once per job is the documented way: `--tab` opens a tab in
        the last-opened window, so they still land together.
        """
        return Plan(
            self.key,
            [
                [
                    "gnome-terminal",
                    "--tab",
                    f"--title={job.label}",
                    f"--working-directory={job.cwd}",
                    "--",
                    "sh",
                    "-c",
                    _posix_payload(job),
                ]
                for job in jobs
            ],
        )


class Konsole(Terminal):
    key = "konsole"
    label = "Konsole"
    platforms = ("linux",)
    tabs = True

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("konsole"))

    def plan(self, jobs: list[Job], **_) -> Plan:
        return Plan(
            self.key,
            [
                ["konsole", "--new-tab", "--workdir", job.cwd, "-e", "sh", "-c", _posix_payload(job)]
                for job in jobs
            ],
        )


class XfceTerminal(Terminal):
    key = "xfce4-terminal"
    label = "Xfce Terminal"
    platforms = ("linux",)
    tabs = True

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("xfce4-terminal"))

    def plan(self, jobs: list[Job], **_) -> Plan:
        argv = ["xfce4-terminal"]
        for job in jobs:
            argv += [
                "--tab",
                f"--title={job.label}",
                f"--working-directory={job.cwd}",
                f"--command=sh -c {shlex.quote(_posix_payload(job))}",
            ]
        return Plan(self.key, [argv])


class _SimpleUnixTerminal(Terminal):
    """A terminal that takes a working directory and a command, one window each."""

    binary = ""
    directory_flag: tuple[str, ...] = ()
    command_flag: tuple[str, ...] = ()
    platforms = ("linux", "darwin")

    def available(self) -> bool:
        return self.supported() and bool(shutil.which(self.binary))

    def plan(self, jobs: list[Job], **_) -> Plan:
        commands = []
        for job in jobs:
            argv = [self.binary, *self.directory_flag, job.cwd, *self.command_flag]
            commands.append(argv + ["sh", "-c", _posix_payload(job)])
        return Plan(self.key, commands)


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

    def plan(self, jobs: list[Job], **_) -> Plan:
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

    def plan(self, jobs: list[Job], **_) -> Plan:
        return Plan(
            self.key,
            [["xterm", "-T", job.label, "-e", "sh", "-c", _posix_payload(job)] for job in jobs],
        )


class Tmux(Terminal):
    """One tmux window per session.

    The natural home for anyone reviving sessions over SSH, and the only backend
    that survives the terminal emulator itself going away.
    """

    key = "tmux"
    label = "tmux"
    platforms = ("linux", "darwin", "nt")
    tabs = True

    def available(self) -> bool:
        return bool(shutil.which("tmux"))

    #: Named so it can be killed again, and so an existing session has nothing
    #: matching it. Only ever created by the ensure step below.
    BOOT_WINDOW = "revenant-boot"

    def plan(self, jobs: list[Job], *, session: str = "revenant", **_) -> Plan:
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
        return Plan(
            self.key, commands, note, optional=frozenset(optional), overhead=frozenset(overhead)
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


def choose(preferred: str | None = None) -> Terminal:
    """Pick a terminal: the requested one, tmux when we are already inside it, or the best available."""
    if preferred:
        try:
            terminal = BY_KEY[preferred]()
        except KeyError:
            known = ", ".join(sorted(BY_KEY))
            raise SystemExit(f"Unknown terminal {preferred!r}. Known: {known}") from None
        return terminal

    if os.environ.get("TMUX") and Tmux().available():
        return Tmux()
    for candidate in ORDER.get(here(), ()):
        terminal = candidate()
        if terminal.available():
            return terminal
    if Tmux().available():
        return Tmux()
    return WindowsConsole() if WINDOWS else Xterm()


def fallbacks(after: Terminal) -> list[Terminal]:
    """Backends worth trying when `after` refuses to start.

    Windows Terminal is the case that matters: it is a Store app-execution alias
    inside an ACL-locked folder, so it can be present, look available, and still
    fail with an access error the moment it is run.
    """
    order = [cls() for cls in ORDER.get(here(), ())]
    if Tmux not in ORDER.get(here(), ()):
        order.append(Tmux())
    seen = [t for t in order if t.key != after.key and t.available()]
    return seen


def available_terminals() -> list[Terminal]:
    seen = [cls() for cls in ALL]
    return [terminal for terminal in seen if terminal.available()]


def run(plan: Plan) -> tuple[int, str]:
    """Execute a plan. Returns (opened, message).

    A terminal that spawns and then quits leaves nothing on screen, which is how
    Windows Terminal fails on a bad profile or an unreadable directory, so the
    started processes get a moment to fall over before any of them is counted.
    """
    # Each console gets its own window instead of fighting over the parent's.
    creation = 0x00000010 if (plan.new_console and WINDOWS) else 0

    opened, failures = 0, []
    started: list[subprocess.Popen] = []
    for index, argv in enumerate(plan.commands):
        cwd = plan.directory(index) or None
        try:
            if plan.terminal == "tmux":
                subprocess.run(argv, check=True, capture_output=True, timeout=30)
                if index not in plan.overhead:
                    opened += 1
            else:
                started.append(
                    subprocess.Popen(argv, cwd=cwd, close_fds=True, creationflags=creation)
                )
        except (OSError, subprocess.SubprocessError) as exc:
            if index not in plan.optional:
                failures.append(str(exc))

    deadline = time.monotonic() + EARLY_EXIT_GRACE
    for process in started:
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            opened += 1  # still up, so it is a window on screen
            continue
        if code == 0:
            opened += 1  # a launcher that handed off and returned
        else:
            failures.append(f"{plan.terminal} exited with status {code}")

    if failures:
        return opened, "; ".join(failures[:3])
    return opened, plan.note
