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
from dataclasses import dataclass, field
from pathlib import Path

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

    def render(self) -> str:
        return "\n".join(shlex.join(argv) for argv in self.commands)


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

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job], *, window: str = "new", profile: str | None = None) -> Plan:
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

    def plan(self, jobs: list[Job]) -> Plan:
        shell = _powershell()
        return Plan(
            self.key,
            [
                ["cmd", "/c", "start", "", "/D", job.cwd, shell, "-NoExit", "-Command", job.command]
                for job in jobs
            ],
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

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job]) -> Plan:
        argv = ["gnome-terminal"]
        for job in jobs:
            argv += [
                "--tab",
                f"--title={job.label}",
                f"--working-directory={job.cwd}",
                "--",
                "sh",
                "-c",
                _posix_payload(job),
            ]
        return Plan(self.key, [argv])


class Konsole(Terminal):
    key = "konsole"
    label = "Konsole"
    platforms = ("linux",)
    tabs = True

    def available(self) -> bool:
        return self.supported() and bool(shutil.which("konsole"))

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job]) -> Plan:
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
    directory_flag = ("--working-directory=",)
    command_flag = ()

    def plan(self, jobs: list[Job]) -> Plan:
        return Plan(
            self.key,
            [
                [
                    "ghostty",
                    f"--working-directory={job.cwd}",
                    f"-e=sh -c {shlex.quote(_posix_payload(job))}",
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

    def plan(self, jobs: list[Job]) -> Plan:
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

    def plan(self, jobs: list[Job], *, session: str = "revenant") -> Plan:
        inside = bool(os.environ.get("TMUX"))
        commands: list[list[str]] = []
        for index, job in enumerate(jobs):
            payload = _posix_payload(job)
            if index == 0 and not inside:
                commands.append(
                    ["tmux", "new-session", "-d", "-s", session, "-n", job.label, "-c", job.cwd, payload]
                )
            else:
                target = ["-t", session] if not inside else []
                commands.append(
                    ["tmux", "new-window", *target, "-n", job.label, "-c", job.cwd, payload]
                )
        note = "" if inside else f"Attach with: tmux attach -t {session}"
        return Plan(self.key, commands, note)


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
    """Execute a plan. Returns (opened, message)."""
    opened, failures = 0, []
    for argv in plan.commands:
        try:
            if plan.terminal in {"tmux"}:
                subprocess.run(argv, check=True, capture_output=True, timeout=30)
            else:
                subprocess.Popen(argv, close_fds=True)
            opened += 1
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(str(exc))
    if failures:
        return opened, "; ".join(failures[:3])
    return opened, plan.note
