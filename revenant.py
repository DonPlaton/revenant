#!/usr/bin/env python3
"""Revenant - bring your agent sessions back from the dead.

Finds every coding-agent session that was active in a chosen time window and
restores it: a readable table, paste-ready `cd` + resume command pairs, a
launcher script, or one terminal tab per session.

The agents themselves live in `revenant_agents.py`, the terminals in
`revenant_terminals.py`, and the desktop app in `revenant_gui.py`. This module is
the discovery, the filtering and the command line.

Safety contract: Revenant never signals, kills, or writes to a running session.
Sessions whose process is still alive are detected and held back by default.

Zero dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence

import revenant_terminals as terminals
from revenant_agents import AGENTS, Agent, get_agent, installed_agents, is_meaningful
from revenant_agents import DEFAULT_AGENT as CLAUDE_CODE

__version__ = "1.2.0"
APP_NAME = "Revenant"

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def state_dir() -> Path:
    """Where Revenant keeps its own files, never inside an agent's config."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME.lower()


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


def _squash(text: str) -> str:
    """Letters and digits only, folded, for comparing two names for sameness."""
    return "".join(char for char in text.lower() if char.isalnum())


@dataclass
class Session:
    """One agent conversation reconstructed from disk."""

    session_id: str
    transcript: Path
    project_slug: str
    agent: Agent = CLAUDE_CODE
    cwd: Path | None = None
    started_at: datetime | None = None
    last_active: datetime | None = None
    turns: int | None = None
    first_prompt: str = ""
    last_prompt: str = ""
    #: What the agent calls this session: a name the user set, else a generated title.
    title: str = ""
    git_branch: str | None = None
    version: str | None = None
    size_bytes: int = 0
    live_pid: int | None = None
    live_name: str | None = None
    live_status: str | None = None
    #: Why this session is being held back, or None when it is safe to revive.
    live_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.live_reason is not None

    @property
    def summary(self) -> str:
        """One line describing the session, best source first.

        The agent's own title beats the last prompt, which is often "continue" and
        says nothing about what the session was for. A title that only repeats the
        directory already in the next column earns its place back to the prompt.
        """
        prompt = self.last_prompt or self.first_prompt
        if self.title and _squash(self.title) != _squash(self.label):
            return self.title
        return prompt or self.title

    @property
    def label(self) -> str:
        """Short human handle: the running session's name, else the directory.

        The flavour is picked from the separator actually present, because a config
        directory written on Windows is readable on Linux, where a backslash is an
        ordinary character.
        """
        if self.live_name:
            return self.live_name
        if self.cwd is not None:
            text = str(self.cwd)
            flavour = PurePosixPath if "/" in text else PureWindowsPath
            return flavour(text).name or text or self.project_slug
        return self.project_slug

    @property
    def resume_command(self) -> str:
        return self.agent.resume_command(self.session_id)

    def job(self) -> terminals.Job:
        return terminals.Job(self.label, str(self.cwd), self.resume_command)


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #


class BadTimeWindow(ValueError):
    """An unreadable `--since` / `--until`. `main` turns it into a usage error."""


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse `24h`, `90m`, `7d`, `2026-09-01`, or `2026-09-01T10:30` into a UTC datetime.

    Bare durations mean "that long ago"; bare dates and datetimes are read in local
    time and converted to UTC.
    """
    now = now or datetime.now(timezone.utc)
    text = value.strip()

    match = _DURATION_RE.match(text)
    if match:
        amount, unit = float(match.group(1)), match.group(2).lower()
        return now - timedelta(seconds=amount * _DURATION_UNITS[unit])

    if text.lower() in {"today", "сегодня"}:
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)
    if text.lower() in {"all", "any", "forever"}:
        return datetime.fromtimestamp(0, tz=timezone.utc)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadTimeWindow(
            f"cannot read time {value!r}; use 24h, 7d, 2026-09-01 or 2026-09-01T10:30"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def humanize_age(moment: datetime | None, *, now: datetime | None = None) -> str:
    """Render a timestamp as a compact age such as `12m`, `3h`, `2d`."""
    if moment is None:
        return "?"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 86400 * 14:
        return f"{seconds // 86400}d"
    return f"{seconds // 604800}w"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def config_root(explicit: str | os.PathLike[str] | None = None, *, agent: Agent = CLAUDE_CODE) -> Path:
    return agent.config_dir(explicit)


#: Installers rename the running binary while they replace it, so the image name
#: of a perfectly healthy session can be `claude.exe.old.1788301984027`.
_RENAMED = re.compile(r"\.(old|new|bak|tmp)(\.\d+)?$")


def _states_windows(pids: Sequence[int]) -> dict[int, str | None]:
    """Ask the kernel about each pid: its image name, None when it exists but will
    not say, and absent when there is no such process.

    `tasklist` costs about half a second and walks every process on the machine.
    This asks about the handful in the registry and costs microseconds.
    """
    import ctypes
    from ctypes import wintypes

    QUERY_LIMITED_INFORMATION = 0x1000
    ACCESS_DENIED = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )

    found: dict[int, str | None] = {}
    for pid in pids:
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Denied means the process is there and guarded, which still counts.
            if ctypes.get_last_error() == ACCESS_DENIED:
                found[pid] = None
            continue
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                found[pid] = PureWindowsPath(buffer.value).name.lower()
            else:
                found[pid] = None
        finally:
            kernel32.CloseHandle(handle)
    return found


def _states_posix(pids: Sequence[int]) -> dict[int, str | None]:
    """Read `/proc` where it exists, otherwise ask `ps` about these pids only."""
    found: dict[int, str | None] = {}
    if Path("/proc").is_dir():
        for pid in pids:
            if not Path(f"/proc/{pid}").is_dir():
                continue
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
            except OSError:
                comm = ""
            # An unreadable or empty name is not evidence of anything, and must not
            # read as "some other program", which would clear the session.
            found[pid] = comm or None
        return found

    if not pids:
        return found
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=,comm=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {pid: None for pid in pids if _pid_alive(pid)}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            try:
                found[int(parts[0])] = PurePosixPath(parts[1].strip()).name.lower()
            except ValueError:
                continue
    return found


def process_states(pids: Sequence[int]) -> dict[int, str | None]:
    """Which of these pids exist, and what each one is running.

    A pid missing from the result has no process. A pid mapped to None exists but
    would not name itself, which is enough to keep its session out of harm's way.
    """
    unique = sorted({int(pid) for pid in pids})
    if not unique:
        return {}
    try:
        return _states_windows(unique) if os.name == "nt" else _states_posix(unique)
    except Exception:  # noqa: BLE001 - a liveness check must never break a scan
        return {pid: None for pid in unique if _pid_alive(pid)}


def looks_like_agent(agent: Agent, name: str) -> bool:
    """Is this executable plausibly the agent?

    Deliberately generous. An installer renames the running binary while it
    replaces it, `/proc/<pid>/comm` is truncated to fifteen characters, and people
    launch agents through wrappers, so an exact list of names would eventually
    clear a running session for revival and corrupt its transcript. Saying yes to
    something that is not the agent only costs a session left off the list.
    """
    base = _RENAMED.sub("", name.lower())
    if base in agent.process_images:
        return True
    stem = base.split(".", 1)[0]
    if not stem:
        return True
    stems = {image.split(".", 1)[0] for image in agent.process_images}
    return any(stem.startswith(known) or known.startswith(stem) for known in stems)


def _pid_alive(pid: int) -> bool:
    """Fallback liveness check when no process table is available.

    Never probes on Windows. There `os.kill(pid, 0)` is not a probe at all: CPython
    opens the process with PROCESS_ALL_ACCESS and calls TerminateProcess for every
    signal but Ctrl-C and Ctrl-Break, so asking whether a session is alive would
    end it. Reporting every pid as alive is the safe answer, and this is only
    reached once the real check has already failed.
    """
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except TypeError:
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def load_live_registry(root: Path, *, agent: Agent = CLAUDE_CODE) -> dict[str, dict]:
    """Sessions the agent registered as running, filtered down to live processes."""
    records = agent.live_registry(root)
    if not records:
        return {}

    wanted: dict[str, int] = {}
    for session_id, record in records.items():
        try:
            wanted[session_id] = int(record["pid"])
        except (TypeError, ValueError, KeyError):
            continue  # a hand-edited or truncated registry file, not a live session

    states = process_states(wanted.values())
    live: dict[str, dict] = {}
    for session_id, pid in wanted.items():
        if pid not in states:
            continue  # no such process, so the registry entry is stale
        name = states[pid]
        # Only a positive identification of some other program clears a session for
        # revival. Anything we cannot read is held back, because relaunching a live
        # session corrupts its transcript and being wrong the other way is a delay.
        if name is not None and not looks_like_agent(agent, name):
            continue
        live[session_id] = records[session_id]
    return live


def scan_sessions(
    root: Path,
    *,
    since: datetime,
    until: datetime | None = None,
    slug_filter: str | None = None,
    agent: Agent = CLAUDE_CODE,
    now: datetime | None = None,
) -> list[Session]:
    """Collect every transcript whose last activity falls inside the window."""
    if not root.is_dir():
        return []

    now = now or datetime.now(timezone.utc)
    history = agent.history(root)
    live = load_live_registry(root, agent=agent)
    sessions: list[Session] = []

    for transcript in agent.transcripts(root):
        slug = agent.group(transcript)
        if slug_filter and slug_filter.lower() not in slug.lower():
            continue
        try:
            stat = transcript.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        session_id = agent.session_id(transcript)
        entries = history.get(session_id, [])
        last_active = max([mtime, *(item[0] for item in entries)]) if entries else mtime
        if last_active < since or (until and last_active > until):
            continue

        session = Session(
            session_id=session_id,
            transcript=transcript,
            project_slug=slug,
            agent=agent,
            last_active=last_active,
            size_bytes=stat.st_size,
        )

        meta = agent.head(transcript)
        if meta.get("cwd"):
            session.cwd = Path(meta["cwd"])
        session.version = meta.get("version")
        session.git_branch = meta.get("gitBranch")
        session.started_at = meta.get("started")

        if entries:
            meaningful = [prompt for _, prompt, _ in entries if is_meaningful(prompt)]
            session.turns = len(meaningful)
            session.first_prompt = meaningful[0] if meaningful else entries[0][1]
            session.last_prompt = meaningful[-1] if meaningful else entries[-1][1]
            if session.cwd is None and entries[0][2]:
                session.cwd = Path(entries[0][2])
        else:
            first, last, count, complete = agent.tail(transcript)
            session.first_prompt, session.last_prompt = first, last
            # An incomplete tail gives only a lower bound, so leave it unknown.
            session.turns = count if complete else None

        registry = live.get(session_id)
        if registry:
            session.live_pid = registry.get("pid")
            session.live_name = registry.get("name")
            session.live_status = registry.get("status")
            session.live_reason = f"process {registry.get('pid')}"
            if session.cwd is None and registry.get("cwd"):
                session.cwd = Path(registry["cwd"])
        elif agent.live_window and (now - mtime).total_seconds() < agent.live_window:
            session.live_reason = "active moments ago"

        sessions.append(session)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    sessions.sort(key=lambda item: item.last_active or epoch, reverse=True)
    return sessions


def scan_all(
    *,
    since: datetime,
    until: datetime | None = None,
    agents: Sequence[Agent] | None = None,
    slug_filter: str | None = None,
) -> list[Session]:
    """Scan every installed agent and merge the results, newest first."""
    found: list[Session] = []
    for agent in agents if agents is not None else installed_agents():
        found += scan_sessions(
            agent.config_dir(), since=since, until=until, agent=agent, slug_filter=slug_filter
        )
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    found.sort(key=lambda item: item.last_active or epoch, reverse=True)
    return found


def name_sessions(sessions: Sequence[Session]) -> None:
    """Fill in each session's title, in place.

    Kept out of the scan because it reads the end of every transcript it is given.
    Call it once the list has been filtered down to what will actually be shown.
    """
    by_agent: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        # The scan fills this in whenever it had to read the tail anyway, and the
        # desktop app calls this again on every request over the same objects.
        if not session.title:
            by_agent[session.agent.key].append(session)
    for group in by_agent.values():
        agent = group[0].agent
        indexed: dict[Path, dict[str, str]] = {}
        for session in group:
            root = agent.root_of(session.transcript)
            if root not in indexed:
                indexed[root] = agent.titles(root)
            named = indexed[root].get(session.session_id)
            session.title = named or agent.title(session.transcript)


def filter_sessions(
    sessions: Sequence[Session],
    *,
    include_live: bool = False,
    only_live: bool = False,
    dirs: Sequence[str] = (),
    min_turns: int = 1,
    latest_per_dir: bool = False,
    limit: int | None = None,
) -> list[Session]:
    """Apply the user's selection rules, newest first."""
    selected: list[Session] = []
    for session in sessions:
        if only_live and not session.is_live:
            continue
        if session.is_live and not (include_live or only_live):
            continue
        if session.turns is not None and session.turns < min_turns:
            continue
        if dirs:
            haystack = str(session.cwd or session.project_slug).lower()
            if not any(needle.lower() in haystack for needle in dirs):
                continue
        selected.append(session)

    if latest_per_dir:
        seen: set[str] = set()
        deduped: list[Session] = []
        for session in selected:  # already newest-first
            key = f"{session.agent.key}:{str(session.cwd or session.project_slug).lower()}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(session)
        selected = deduped

    return selected[:limit] if limit else selected


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #


def snapshot_path(root: Path | None = None, *, agent: Agent = CLAUDE_CODE) -> Path:
    """Snapshots are per (agent, config root); two roots must not share one file."""
    if root is None:
        return state_dir() / f"snapshot-{agent.key}.json"
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    return state_dir() / f"snapshot-{agent.key}-{digest}.json"


def write_snapshot(root: Path, *, agent: Agent = CLAUDE_CODE) -> dict:
    """Record the currently running sessions so a crash can be undone exactly.

    Optional: transcripts alone already reconstruct the window. A snapshot adds
    precision by separating "was running when the machine died" from "I closed that
    one on purpose an hour ago".
    """
    live = load_live_registry(root, agent=agent)
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "agent": agent.key,
        "sessions": [
            {
                "sessionId": session_id,
                "cwd": record.get("cwd"),
                "name": record.get("name"),
                "status": record.get("status"),
                "pid": record.get("pid"),
                "startedAt": record.get("startedAt"),
            }
            for session_id, record in live.items()
        ],
    }
    target = snapshot_path(root, agent=agent)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return payload


def read_snapshot(root: Path | None = None, *, agent: Agent = CLAUDE_CODE) -> dict | None:
    path = snapshot_path(root, agent=agent)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _use_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Palette:
    """ANSI codes, or empty strings when the output is piped."""

    def __init__(self, enabled: bool) -> None:
        self.dim = "\033[2m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.yellow = "\033[33m" if enabled else ""
        self.cyan = "\033[36m" if enabled else ""
        self.reset = "\033[0m" if enabled else ""


def _display_width(text: str) -> int:
    """Width in terminal cells, counting CJK and emoji as two."""
    width = 0
    for char in text:
        code = ord(char)
        if 0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7A3:
            width += 2
        elif 0xF900 <= code <= 0xFAFF or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60:
            width += 2
        elif 0x1F300 <= code <= 0x1FAFF:
            width += 2
        else:
            width += 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    out, used = "", 0
    for char in text:
        step = _display_width(char)
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "…"


def render_table(sessions: Sequence[Session], *, stream=None, now: datetime | None = None) -> None:
    """Print the numbered session table used by every non-machine output mode."""
    stream = stream if stream is not None else sys.stdout
    palette = Palette(_use_color(stream))
    if not sessions:
        print("No sessions in this window. Widen it with --since 7d.", file=stream)
        return

    multi_agent = len({s.agent.key for s in sessions}) > 1
    terminal = shutil.get_terminal_size((120, 25)).columns
    index_w = max(2, len(str(len(sessions))))
    age_w, turns_w = 5, 5
    agent_w = max((len(s.agent.label) for s in sessions), default=0) if multi_agent else 0
    label_w = min(26, max(6, max(_display_width(s.label) for s in sessions)))
    fixed = index_w + age_w + turns_w + label_w + agent_w + 11
    prompt_w = max(20, terminal - fixed - 2)

    header = f"{palette.dim}{_pad('#', index_w)}  {_pad('AGE', age_w)} {_pad('TURNS', turns_w)} "
    if multi_agent:
        header += f"{_pad('AGENT', agent_w)} "
    header += f"{_pad('SESSION', label_w)}  ABOUT{palette.reset}"
    print(header, file=stream)

    for number, session in enumerate(sessions, start=1):
        age = humanize_age(session.last_active, now=now)
        turns = str(session.turns) if session.turns is not None else "?"
        mark = f"{palette.green}*{palette.reset}" if session.is_live else " "
        prompt = _truncate(session.summary or "-", prompt_w)
        row = f"{_pad(str(number), index_w)}{mark} {_pad(age, age_w)} {_pad(turns, turns_w)} "
        if multi_agent:
            row += f"{palette.cyan}{_pad(session.agent.label, agent_w)}{palette.reset} "
        row += (
            f"{palette.bold}{_pad(_truncate(session.label, label_w), label_w)}{palette.reset}  "
            f"{palette.dim}{prompt}{palette.reset}"
        )
        print(row, file=stream)

    print(file=stream)
    for number, session in enumerate(sessions, start=1):
        cwd = str(session.cwd) if session.cwd else f"<unknown: {session.project_slug}>"
        suffix = ""
        if session.is_live:
            suffix = f"  {palette.yellow}[held back: {session.live_reason}]{palette.reset}"
        print(f"{palette.dim}{number}.{palette.reset} {cwd}{suffix}", file=stream)

    live_count = sum(1 for s in sessions if s.is_live)
    print(
        f"\n{palette.cyan}{len(sessions)} session(s)"
        + (f", {live_count} still running" if live_count else "")
        + f".{palette.reset}",
        file=stream,
    )


def _quote_ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _quote_sh(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def render_commands(sessions: Sequence[Session], *, shell: str = "pwsh") -> str:
    """Produce the paste-ready `cd` + resume command pairs."""
    lines: list[str] = []
    for session in sessions:
        cwd = str(session.cwd) if session.cwd else ""
        comment = session.summary or session.label
        if shell == "cmd":
            lines.append(f":: {session.label} - {comment}")
            lines.append(f'cd /d "{cwd}"' if cwd else ":: unknown directory")
        elif shell == "bash":
            lines.append(f"# {session.label} - {comment}")
            lines.append(f"cd {_quote_sh(cwd)}" if cwd else "# unknown directory")
        else:
            lines.append(f"# {session.label} - {comment}")
            lines.append(f"cd {_quote_ps(cwd)}" if cwd else "# unknown directory")
        lines.append(session.resume_command)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_ps_plan(plan: terminals.Plan) -> list[str]:
    """A plan as PowerShell, one Start-Process per command.

    Start-Process takes the working directory as a parameter, so a path holding
    shell punctuation never has to survive being parsed a second time.
    """
    lines: list[str] = []
    for index, argv in enumerate(plan.commands):
        call = f"Start-Process {_quote_ps(argv[0])}"
        cwd = plan.directory(index)
        if cwd:
            call += f" -WorkingDirectory {_quote_ps(cwd)}"
        if argv[1:]:
            call += " -ArgumentList " + ", ".join(_quote_ps(part) for part in argv[1:])
        lines.append(f"  try {{ {call} }} catch {{ }}" if index in plan.optional else f"  {call}")
    return lines


def render_launcher(
    sessions: Sequence[Session],
    *,
    shell: str = "pwsh",
    terminal: terminals.Terminal | None = None,
    window: str = "new",
    profile: str | None = None,
) -> str:
    """Build a script that opens every session in its own terminal tab."""
    usable = [session for session in sessions if session.cwd]
    jobs = [session.job() for session in usable]
    skipped = [
        f"# skipped {session.session_id}: unknown directory"
        for session in sessions
        if not session.cwd
    ]

    if shell == "cmd":
        head = ["@echo off", "rem generated by Revenant", ""]
        body = [
            # `start /D` takes the directory; a repr-quoted path breaks it. Doubling
            # the percent signs stops a path like C:\100%done being read as a variable.
            'start "" /D "{0}" cmd /k {1}'.format(
                str(session.cwd).replace("%", "%%"), session.resume_command
            )
            for session in usable
        ]
        tail = [line.replace("#", "::", 1) for line in skipped]
        return "\n".join(head + body + tail) + "\n"

    if shell == "bash":
        chosen = terminal or terminals.choose()
        head = [
            "#!/usr/bin/env bash",
            f"# generated by Revenant for {chosen.label}",
            "set -euo pipefail",
            "",
        ]
        if not usable:
            return "\n".join(head + skipped + ["echo 'Revenant: nothing to restore'"]) + "\n"
        plan = chosen.plan(jobs, window=window, profile=profile)
        body = [plan.render()]
        if plan.note:
            body.append(f"echo {_quote_sh(plan.note)}")
        return "\n".join(head + skipped + ([""] if skipped else []) + body) + "\n"

    # A terminal the user named that is not Windows Terminal has its own plan, and
    # the tabbed wt.exe form below cannot express it.
    if terminal is not None and not isinstance(terminal, terminals.WindowsTerminal):
        head = [
            "# generated by Revenant",
            f"# Opens every session with {terminal.label}.",
            "",
            "$ErrorActionPreference = 'Stop'",
            "",
        ]
        if not usable:
            return "\n".join(head + skipped + ["Write-Host 'Revenant: nothing to restore'"]) + "\n"
        plan = terminal.plan(jobs, window=window, profile=profile)
        body = _render_ps_plan(plan)
        if plan.note:
            body.append(f"Write-Host {_quote_ps(plan.note)}")
        return "\n".join(head + skipped + ([""] if skipped else []) + body) + "\n"

    head = [
        "# generated by Revenant",
        "# Opens every session as a tab in one Windows Terminal window.",
        "",
        "$ErrorActionPreference = 'Stop'",
        "",
    ]
    if not usable:
        return "\n".join(head + skipped + ["Write-Host 'Revenant: nothing to restore'"]) + "\n"

    # One wt.exe call with `;`-separated tabs; the semicolons belong to wt, so
    # PowerShell must not eat them, hence the backtick escape.
    tab_profile = f"-p {_quote_ps(profile)} " if profile else ""
    parts: list[str] = [f"  wt.exe -w {_quote_ps(window)}"]
    for index, session in enumerate(usable):
        prefix = "    " if index == 0 else "    `; "
        parts.append(
            f"{prefix}new-tab {tab_profile}--title {_quote_ps(session.label)} "
            f"-d {_quote_ps(str(session.cwd))} "
            f"$shell -NoExit -Command {_quote_ps(session.resume_command)}"
        )
    invocation = " `\n".join(parts)

    # wt.exe is a Store alias in an ACL-locked folder; some shells cannot run it.
    fallback = [
        "  if ($LASTEXITCODE -ne 0) { throw 'wt.exe failed' }",
        "} catch {",
        "  Write-Host 'Windows Terminal unavailable, opening one window per session.'",
        "  $failed = $true",
        "}",
        "",
        "if ($failed) {",
    ]
    for session in usable:
        fallback.append(
            f"  Start-Process $shell -WorkingDirectory {_quote_ps(str(session.cwd))} "
            f"-ArgumentList '-NoExit', '-Command', {_quote_ps(session.resume_command)}"
        )
    fallback.append("}")

    prologue = [
        "$shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }",
        "$failed = $false",
        "",
        "try {",
    ]
    return (
        "\n".join(head + skipped + ([""] if skipped else []) + prologue + [invocation] + fallback)
        + "\n"
    )


# --------------------------------------------------------------------------- #
# launching
# --------------------------------------------------------------------------- #


def plan_launch(
    sessions: Sequence[Session], *, terminal: str | None = None, window: str = "new", profile: str | None = None
) -> tuple[terminals.Terminal, terminals.Plan]:
    """Choose a terminal and build the commands, without running anything."""
    chosen = terminals.choose(terminal)
    jobs = [session.job() for session in sessions if session.cwd]
    # Options a backend does not use are ignored by its own signature, so every
    # backend, including one we fall back to, is offered all of them.
    return chosen, chosen.plan(jobs, window=window, profile=profile)


def launch(
    sessions: Sequence[Session],
    *,
    terminal: str | None = None,
    window: str = "new",
    profile: str | None = None,
    dry_run: bool = False,
    stream=None,
) -> int:
    """Open the selected sessions in a terminal."""
    stream = stream if stream is not None else sys.stdout
    usable = [s for s in sessions if s.cwd]
    if not usable:
        print("Nothing to launch: no session has a known directory.", file=stream)
        return 1

    live = [s for s in usable if s.is_live]
    if live:
        # Held back, not a reason to abandon the rest: the desktop app has always
        # opened what it safely could, and refusing the whole set here meant the two
        # front ends disagreed about the same rule.
        names = ", ".join(f"{s.label} ({s.live_reason})" for s in live)
        print(
            f"Holding back {len(live)} session(s) that may still be running: {names}.\n"
            "Two processes on one transcript corrupt it. Close them and run this again.",
            file=stream,
        )
        usable = [s for s in usable if not s.is_live]
        if not usable:
            return 2
        print(file=stream)

    chosen, plan = plan_launch(usable, terminal=terminal, window=window, profile=profile)
    if dry_run:
        print(plan.render(), file=stream)
        return 0

    jobs = [session.job() for session in usable]
    opened, message = terminals.run(plan)
    if not opened and not terminal:
        # A terminal can look available and still refuse to run, so try the next one.
        for candidate in terminals.fallbacks(chosen):
            print(f"{chosen.label} would not start ({message}); trying {candidate.label}.", file=stream)
            chosen = candidate
            plan = candidate.plan(jobs, window=window, profile=profile)
            opened, message = terminals.run(plan)
            if opened:
                break
    if not opened:
        print(f"{chosen.label} would not start: {message}", file=stream)
        return 3

    where = "tab" if chosen.tabs else "window"
    count = len(usable) if chosen.tabs and len(plan.commands) == 1 else opened
    print(f"Opened {count} {where}(s) in {chosen.label}.", file=stream)
    if message:
        print(message, file=stream)
    return 0


def pick(sessions: Sequence[Session], *, stream=None, reader=input) -> list[Session]:
    """Ask which of the listed sessions to act on. `all` or empty selects everything."""
    stream = stream if stream is not None else sys.stdout
    if not sessions:
        return []
    print(
        "\nWhich sessions? e.g. 1,3,5 or 1-4 or all (Enter = all, q = quit): ",
        end="",
        file=stream,
        flush=True,
    )
    try:
        answer = reader("").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=stream)
        return []
    if answer.lower() in {"q", "quit", "n", "no"}:
        return []
    if not answer or answer.lower() in {"all", "*", "a"}:
        return list(sessions)

    chosen: list[int] = []
    for chunk in answer.replace(" ", ",").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                first, last = int(start), int(end)
            except ValueError:
                continue
            # Clamped before the range is built: a mistyped `1-999999999` would
            # otherwise allocate a billion integers to then throw all but a few away.
            chosen.extend(range(max(1, first), min(last, len(sessions)) + 1))
        else:
            try:
                chosen.append(int(chunk))
            except ValueError:
                continue
    return [sessions[i - 1] for i in dict.fromkeys(chosen) if 1 <= i <= len(sessions)]


def session_to_dict(session: Session) -> dict:
    """Serialise one session, shared by `--json` and the desktop UI."""
    return {
        "sessionId": session.session_id,
        "agent": session.agent.key,
        "agentLabel": session.agent.label,
        "cwd": str(session.cwd) if session.cwd else None,
        "label": session.label,
        "lastActive": session.last_active.isoformat() if session.last_active else None,
        "ageLabel": humanize_age(session.last_active),
        "startedAt": session.started_at.isoformat() if session.started_at else None,
        "turns": session.turns,
        "title": session.title,
        "summary": session.summary,
        "firstPrompt": session.first_prompt,
        "lastPrompt": session.last_prompt,
        "gitBranch": session.git_branch,
        "version": session.version,
        "sizeBytes": session.size_bytes,
        "live": session.is_live,
        "liveReason": session.live_reason,
        "pid": session.live_pid,
        "resume": session.resume_command,
        "transcript": str(session.transcript),
    }


def to_json(sessions: Sequence[Session]) -> str:
    return json.dumps([session_to_dict(s) for s in sessions], ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revenant",
        description="Bring your agent sessions back from the dead.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  revenant                      sessions from the last 24h\n"
            "  revenant --since 7d --pick    choose from the last week\n"
            "  revenant --since 6h --launch  reopen each in its own terminal tab\n"
            "  revenant --all-agents         every agent installed here\n"
            "  revenant --emit revive.sh     write a launcher script\n"
            "  revenant gui                  open the desktop app\n"
            "  revenant agents               what is installed and where\n"
        ),
    )
    parser.add_argument(
        "command", nargs="?", default="list", choices=["list", "snapshot", "gui", "agents", "terminals"]
    )
    parser.add_argument("--agent", default=None, help=f"which agent to look for ({', '.join(AGENTS)})")
    parser.add_argument("--all-agents", action="store_true", help="scan every agent installed here")
    parser.add_argument("--root", help="agent config dir (default: the agent's own location)")
    parser.add_argument("--since", default="24h", help="window start: 24h, 7d, today, all, 2026-09-01")
    parser.add_argument("--until", help="window end (same formats)")
    parser.add_argument("--dir", action="append", default=[], help="only sessions whose path contains this")
    parser.add_argument("--slug", help="only this transcript folder")
    parser.add_argument("--min-turns", type=int, default=1, help="skip sessions with fewer prompts (default 1)")
    parser.add_argument("--limit", type=int, default=40, help="max sessions to show (default 40, 0 = no limit)")
    parser.add_argument("--latest-per-dir", action="store_true", help="keep only the newest session per directory")
    parser.add_argument("--include-live", action="store_true", help="also show sessions that may still be running")
    parser.add_argument("--only-live", action="store_true", help="show only sessions that may still be running")
    parser.add_argument("--from-snapshot", action="store_true", help="restore the set recorded by `revenant snapshot`")

    output = parser.add_argument_group("output")
    output.add_argument("--print", dest="print_cmds", action="store_true", help="print paste-ready commands")
    output.add_argument("--emit", metavar="FILE", help="write a launcher script (.ps1 / .sh / .cmd)")
    output.add_argument("--launch", action="store_true", help="open each session in a terminal")
    output.add_argument("--pick", action="store_true", help="choose interactively before acting")
    output.add_argument("--dry-run", action="store_true", help="with --launch, show the commands instead")
    output.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    output.add_argument("--shell", choices=["pwsh", "bash", "cmd"], default=None, help="target shell for --print/--emit")
    output.add_argument("--terminal", help=f"where to open sessions ({', '.join(sorted(terminals.BY_KEY))})")
    output.add_argument("--window", default="new", help="wt.exe target window: new, 0, or a name")
    output.add_argument("--profile", help="Windows Terminal profile for new tabs")
    # 1.0 shipped this; --terminal replaced it. Failing outright on an old alias in
    # someone's script is worse than honouring it.
    output.add_argument("--no-tabs", action="store_true", help=argparse.SUPPRESS)
    output.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def _restrict_to_snapshot(sessions: list[Session], snapshot: dict | None, *, stream) -> list[Session]:
    if not snapshot:
        # Falling back to "everything" would silently relaunch the whole window.
        print("No snapshot found. Run `revenant snapshot` while your sessions are open.", file=stream)
        return []
    wanted = {entry.get("sessionId") for entry in snapshot.get("sessions", [])}
    captured = snapshot.get("captured_at", "?")
    filtered = [s for s in sessions if s.session_id in wanted]
    print(f"Snapshot from {captured}: {len(wanted)} recorded, {len(filtered)} found on disk.\n", file=stream)
    return filtered


def _print_agents(stream) -> int:
    for agent in AGENTS.values():
        root = agent.config_dir()
        mark = "installed" if root.is_dir() else "not found"
        print(f"{agent.key:<12} {agent.label:<14} {mark:<11} {root}", file=stream)
        if agent.liveness_note:
            print(f"{'':<12} {agent.liveness_note}", file=stream)
    return 0


def _print_terminals(stream) -> int:
    current = terminals.choose()
    for cls in terminals.ALL:
        terminal = cls()
        if not terminal.supported():
            continue
        state = "available" if terminal.available() else "not installed"
        chosen = "  <- default here" if terminal.key == current.key else ""
        tabs = "tabs" if terminal.tabs else "windows"
        print(f"{terminal.key:<16} {terminal.label:<18} {state:<14} {tabs}{chosen}", file=stream)
    return 0


def main(argv: Sequence[str] | None = None, *, stream=None) -> int:
    stream = stream if stream is not None else sys.stdout
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    if args.no_tabs and not args.terminal:
        # What --no-tabs meant in 1.0: a window per session rather than tabs.
        args.terminal = "conhost" if os.name == "nt" else None
        print("revenant: --no-tabs is deprecated; use --terminal.", file=sys.stderr)
    agent = get_agent(args.agent)
    shell = args.shell or ("pwsh" if os.name == "nt" else "bash")

    if args.command == "agents":
        return _print_agents(stream)
    if args.command == "terminals":
        return _print_terminals(stream)
    if args.command == "gui":
        from revenant_gui import run_gui  # lazy: the CLI itself stays dependency-free

        return run_gui(agent=agent, root=args.root)

    try:
        since = parse_when(args.since)
        until = parse_when(args.until) if args.until else None
    except BadTimeWindow as exc:
        print(f"revenant: {exc}", file=stream)
        return 2

    root = config_root(args.root, agent=agent)

    if args.command == "snapshot":
        if not root.is_dir():
            print(f"{agent.label} config dir not found: {root}", file=stream)
            return 1
        payload = write_snapshot(root, agent=agent)
        print(
            f"Recorded {len(payload['sessions'])} running session(s) to "
            f"{snapshot_path(root, agent=agent)}",
            file=stream,
        )
        for entry in payload["sessions"]:
            print(f"  {entry['name'] or entry['sessionId'][:8]}  {entry['cwd']}", file=stream)
        return 0

    if args.all_agents:
        # Each of these names a single agent, so pairing them with --all-agents asks
        # for two different things at once. Silently honouring one was the old bug.
        for flag, value in (("--agent", args.agent), ("--root", args.root)):
            if value:
                print(f"revenant: {flag} and --all-agents ask for different things.", file=stream)
                return 2
        if args.from_snapshot:
            print("revenant: --from-snapshot restores one agent; drop --all-agents.", file=stream)
            return 2
        sessions = scan_all(since=since, until=until, slug_filter=args.slug)
        if not sessions and not installed_agents():
            print("No agents found on this machine.", file=stream)
            return 1
    else:
        if not root.is_dir():
            print(f"{agent.label} config dir not found: {root}", file=stream)
            return 1
        sessions = scan_sessions(root, since=since, until=until, slug_filter=args.slug, agent=agent)

    if args.from_snapshot:
        # Keep stdout pure when the caller asked for machine-readable output.
        notice = sys.stderr if args.as_json else stream
        sessions = _restrict_to_snapshot(sessions, read_snapshot(root, agent=agent), stream=notice)

    selected = filter_sessions(
        sessions,
        include_live=args.include_live,
        only_live=args.only_live,
        dirs=args.dir,
        min_turns=args.min_turns,
        latest_per_dir=args.latest_per_dir,
        limit=args.limit or None,
    )
    name_sessions(selected)

    if args.as_json:
        print(to_json(selected), file=stream)
        return 0

    render_table(selected, stream=stream)
    if not selected:
        return 0

    acting = args.print_cmds or args.emit or args.launch
    if args.pick and acting:
        selected = pick(selected, stream=stream)
        if not selected:
            print("Nothing selected.", file=stream)
            return 0

    if args.print_cmds:
        print(file=stream)
        print(render_commands(selected, shell=shell), file=stream)

    if args.emit:
        target = Path(args.emit).expanduser()
        by_suffix = {"ps1": "pwsh", "sh": "bash", "bash": "bash", "cmd": "cmd", "bat": "cmd"}
        emit_shell = by_suffix.get(target.suffix.lower().lstrip("."), shell)
        target.parent.mkdir(parents=True, exist_ok=True)
        chosen = terminals.choose(args.terminal) if args.terminal or emit_shell == "bash" else None
        if emit_shell == "cmd" and chosen is not None and chosen.key != "conhost":
            # A .cmd launcher opens console windows and can be nothing else, so
            # writing one that quietly ignored --terminal would be a lie.
            print(f"revenant: a .cmd launcher cannot use {chosen.label}; writing console windows.", file=stream)
        target.write_text(
            render_launcher(
                selected,
                shell=emit_shell,
                terminal=chosen,
                window=args.window,
                profile=args.profile,
            ),
            encoding="utf-8",
        )
        if emit_shell == "bash":
            target.chmod(target.stat().st_mode | 0o111)
        print(f"\nWrote {target} ({len(selected)} session(s)). Run it to bring them back.", file=stream)

    if args.launch:
        return launch(
            selected,
            terminal=args.terminal,
            window=args.window,
            profile=args.profile,
            dry_run=args.dry_run,
            stream=stream,
        )

    if not acting:
        print(
            "\nNext: --print (commands) - --emit revive.sh (script) - --launch (open them) - --pick (choose first)",
            file=stream,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
