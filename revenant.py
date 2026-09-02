#!/usr/bin/env python3
"""Revenant - bring your agent sessions back from the dead.

Finds every coding-agent session that was active in a chosen time window and
restores it: a readable table, paste-ready `cd` + resume command pairs, a
launcher script, or one terminal tab per session.

Claude Code (the first supported agent) stores each conversation as
`<config>/projects/<slug>/<uuid>.jsonl` and registers only *currently running*
sessions in `<config>/sessions/<pid>.json`. The registry is pruned on startup,
so after a crash it is empty - the transcripts are the durable source of truth,
and this tool reads them.

Safety contract: Revenant never signals, kills, or writes to a running session.
Sessions whose process is still alive are detected and excluded by default.

Zero dependencies, stdlib only. The desktop UI lives in `revenant_gui.py`.
"""

from __future__ import annotations

import argparse
import csv
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

__version__ = "1.0.0"
APP_NAME = "Revenant"

# Records at the head of a transcript that carry cwd / version / branch.
_HEAD_LINES = 80
# Bytes read from the end of a transcript when the prompt history has no entry for it.
_TAIL_BYTES = 256 * 1024

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_META_PROMPT_RE = re.compile(r"^\s*<(local-command-caveat|command-name|command-message)")


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Agent:
    """A coding agent whose sessions Revenant knows how to find and resume.

    Only Claude Code ships today; the seam exists so another agent that keeps
    per-conversation transcripts on disk can be added without touching the rest.
    """

    key: str
    label: str
    env_var: str
    default_dir: Path
    resume_template: str
    #: Process images a live session can run under - guards against PID reuse.
    process_images: frozenset[str]

    def config_dir(self, explicit: str | os.PathLike[str] | None = None) -> Path:
        if explicit:
            return Path(explicit).expanduser()
        env = os.environ.get(self.env_var)
        return Path(env).expanduser() if env else self.default_dir

    def resume_command(self, session_id: str) -> str:
        return self.resume_template.format(id=session_id)


CLAUDE_CODE = Agent(
    key="claude-code",
    label="Claude Code",
    env_var="CLAUDE_CONFIG_DIR",
    default_dir=Path.home() / ".claude",
    resume_template="claude --resume {id}",
    process_images=frozenset({"claude.exe", "claude", "node.exe", "node", "bun.exe", "bun"}),
)

AGENTS: dict[str, Agent] = {CLAUDE_CODE.key: CLAUDE_CODE}


def get_agent(key: str | None) -> Agent:
    if not key:
        return CLAUDE_CODE
    try:
        return AGENTS[key]
    except KeyError:
        raise SystemExit(f"Unknown agent {key!r}. Known: {', '.join(sorted(AGENTS))}")


def state_dir() -> Path:
    """Where Revenant keeps its own files - never inside the agent's config."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME.lower()


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


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
    git_branch: str | None = None
    version: str | None = None
    size_bytes: int = 0
    live_pid: int | None = None
    live_name: str | None = None
    live_status: str | None = None

    @property
    def is_live(self) -> bool:
        return self.live_pid is not None

    @property
    def label(self) -> str:
        """Short human handle: the running session's name, else the directory.

        Split on both separators instead of using `Path.name`: a config directory
        written on Windows is readable on Linux, where a backslash is an ordinary
        character and `Path(r"D:\\Coding\\x").name` is the whole string.
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


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #


class BadTimeWindow(ValueError):
    """An unreadable `--since` / `--until`. `main` turns it into a usage error."""


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse `24h`, `90m`, `7d`, `2026-09-01`, or `2026-09-01T10:30` into a UTC datetime.

    Bare durations are interpreted as "that long ago"; bare dates and datetimes
    are read in local time and converted to UTC.
    """
    now = now or datetime.now(timezone.utc)
    text = value.strip()

    match = _DURATION_RE.match(text)
    if match:
        amount, unit = float(match.group(1)), match.group(2).lower()
        return now - timedelta(seconds=amount * _DURATION_UNITS[unit])

    if text.lower() in {"today", "сегодня"}:
        local_midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return local_midnight.astimezone(timezone.utc)
    if text.lower() in {"all", "any", "forever"}:
        return datetime.fromtimestamp(0, tz=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BadTimeWindow(
            f"cannot read time {value!r}; use 24h, 7d, 2026-09-01 or 2026-09-01T10:30"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_epoch_ms(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return None


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
    """Locate the agent's config directory."""
    return agent.config_dir(explicit)


def _clean_prompt(text: object, *, limit: int = 160) -> str:
    """Flatten a prompt to one printable line."""
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _is_meaningful(prompt: str) -> bool:
    """True for prompts a human actually typed, not slash commands or harness meta."""
    if not prompt or _META_PROMPT_RE.match(prompt):
        return False
    return not prompt.startswith("/")


def load_history(root: Path) -> dict[str, list[tuple[datetime, str, str]]]:
    """Index `history.jsonl` as sessionId -> [(timestamp, prompt, project)].

    This file holds every prompt the user typed, is small, and gives turn counts
    and prompt previews without parsing hundreds of megabytes of transcripts.
    """
    path = root / "history.jsonl"
    index: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
    if not path.is_file():
        return index
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("sessionId")
            moment = _from_epoch_ms(record.get("timestamp"))
            if not session_id or moment is None:
                continue
            index[session_id].append(
                (moment, _clean_prompt(record.get("display")), record.get("project") or "")
            )
    for entries in index.values():
        entries.sort(key=lambda item: item[0])
    return index


def _process_table() -> dict[int, str]:
    """Map pid -> image name. Empty on POSIX, where `os.kill(pid, 0)` is used instead."""
    if os.name != "nt":
        return {}
    try:
        completed = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, str] = {}
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) < 2:
            continue
        try:
            found[int(row[1])] = row[0].lower()
        except ValueError:
            continue
    return found


def _pid_alive_posix(pid: int) -> bool:
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
    """Read `<config>/sessions/*.json` and keep only entries with a live process.

    Claude Code writes one file per running session and prunes them on startup,
    so a stale file means a crashed session - which we still want to surface,
    just not as *live*.
    """
    directory = root / "sessions"
    if not directory.is_dir():
        return {}

    records: list[dict] = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("sessionId") and record.get("pid"):
            records.append(record)
    if not records:
        return {}

    table = _process_table()
    live: dict[str, dict] = {}
    for record in records:
        try:
            pid = int(record["pid"])
        except (TypeError, ValueError):
            continue  # a hand-edited or truncated registry file, not a live session
        if os.name == "nt":
            image = table.get(pid)
            # A pruned-then-reused pid would otherwise be reported as live.
            if image is None or image not in agent.process_images:
                continue
        elif not _pid_alive_posix(pid):
            continue
        live[record["sessionId"]] = record
    return live


def _head_metadata(path: Path) -> dict:
    """Pull cwd / version / branch / start time from the first records of a transcript."""
    meta: dict = {}
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return meta
    with handle:
        for index, line in enumerate(handle):
            if index >= _HEAD_LINES and "cwd" in meta:
                break
            if index >= _HEAD_LINES * 8:  # transcript with an unusually long preamble
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            for key in ("cwd", "version", "gitBranch"):
                if key not in meta and record.get(key):
                    meta[key] = record[key]
            if "started" not in meta:
                moment = _from_iso(record.get("timestamp"))
                if moment:
                    meta["started"] = moment
    return meta


def _tail_prompts(path: Path) -> tuple[str, str, int, bool]:
    """Recover (first, last, count, complete) user prompts from a transcript's tail.

    Fallback for sessions missing from history.jsonl (older versions, rotation).
    `complete` is False when the file was larger than the tail window, in which
    case the first prompt and the count describe the tail only and must not be
    reported as the session's own.
    """
    complete = True
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                complete = False
                handle.seek(size - _TAIL_BYTES)
                handle.readline()  # drop the partial line
            blob = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "", "", 0, False

    prompts: list[str] = []
    for line in blob.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        if record.get("isMeta") or record.get("isSidechain"):
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        text = _clean_prompt(content)
        if _is_meaningful(text):
            prompts.append(text)
    if not prompts:
        return "", "", 0, complete
    return (prompts[0] if complete else ""), prompts[-1], len(prompts), complete


def scan_sessions(
    root: Path,
    *,
    since: datetime,
    until: datetime | None = None,
    slug_filter: str | None = None,
    agent: Agent = CLAUDE_CODE,
) -> list[Session]:
    """Collect every transcript whose last activity falls inside the window."""
    projects = root / "projects"
    if not projects.is_dir():
        return []

    history = load_history(root)
    live = load_live_registry(root, agent=agent)
    sessions: list[Session] = []

    for transcript in projects.glob("*/*.jsonl"):
        slug = transcript.parent.name
        if slug_filter and slug_filter.lower() not in slug.lower():
            continue
        try:
            stat = transcript.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        session_id = transcript.stem
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

        meta = _head_metadata(transcript)
        if meta.get("cwd"):
            session.cwd = Path(meta["cwd"])
        session.version = meta.get("version")
        session.git_branch = meta.get("gitBranch")
        session.started_at = meta.get("started")

        if entries:
            meaningful = [prompt for _, prompt, _ in entries if _is_meaningful(prompt)]
            session.turns = len(meaningful)
            session.first_prompt = meaningful[0] if meaningful else entries[0][1]
            session.last_prompt = meaningful[-1] if meaningful else entries[-1][1]
            if session.cwd is None and entries[0][2]:
                session.cwd = Path(entries[0][2])
        else:
            first, last, count, complete = _tail_prompts(transcript)
            session.first_prompt, session.last_prompt = first, last
            # An incomplete tail gives only a lower bound, so leave it unknown.
            session.turns = count if complete else None

        registry = live.get(session_id)
        if registry:
            session.live_pid = registry.get("pid")
            session.live_name = registry.get("name")
            session.live_status = registry.get("status")
            if session.cwd is None and registry.get("cwd"):
                session.cwd = Path(registry["cwd"])

        sessions.append(session)

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    sessions.sort(key=lambda item: item.last_active or epoch, reverse=True)
    return sessions


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
            key = str(session.cwd or session.project_slug).lower()
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
    """Snapshots are per (agent, config root) - two roots must not share one file."""
    if root is None:
        return state_dir() / f"snapshot-{agent.key}.json"
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    return state_dir() / f"snapshot-{agent.key}-{digest}.json"


def write_snapshot(root: Path, *, agent: Agent = CLAUDE_CODE) -> dict:
    """Record the currently running sessions so a crash can be undone exactly.

    Optional: transcripts alone already reconstruct the window. A snapshot adds
    precision - it separates "was running when the machine died" from "I closed
    that one on purpose an hour ago".
    """
    live = load_live_registry(root, agent=agent)
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "agent": agent.key,
        "sessions": [
            {
                "sessionId": record.get("sessionId"),
                "cwd": record.get("cwd"),
                "name": record.get("name"),
                "status": record.get("status"),
                "pid": record.get("pid"),
                "startedAt": record.get("startedAt"),
            }
            for record in live.values()
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
    out = ""
    used = 0
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

    terminal = shutil.get_terminal_size((120, 25)).columns
    index_w = max(2, len(str(len(sessions))))
    age_w = 5
    turns_w = 5
    label_w = min(26, max(6, max(_display_width(s.label) for s in sessions)))
    fixed = index_w + age_w + turns_w + label_w + 10
    prompt_w = max(20, terminal - fixed - 2)

    header = (
        f"{palette.dim}{_pad('#', index_w)}  {_pad('AGE', age_w)} "
        f"{_pad('TURNS', turns_w)} {_pad('SESSION', label_w)}  LAST PROMPT{palette.reset}"
    )
    print(header, file=stream)

    for number, session in enumerate(sessions, start=1):
        age = humanize_age(session.last_active, now=now)
        turns = str(session.turns) if session.turns is not None else "?"
        mark = f"{palette.green}●{palette.reset}" if session.is_live else " "
        prompt = _truncate(session.last_prompt or session.first_prompt or "—", prompt_w)
        print(
            f"{_pad(str(number), index_w)}{mark} {_pad(age, age_w)} "
            f"{_pad(turns, turns_w)} {palette.bold}{_pad(_truncate(session.label, label_w), label_w)}{palette.reset}  "
            f"{palette.dim}{prompt}{palette.reset}",
            file=stream,
        )

    print(file=stream)
    for number, session in enumerate(sessions, start=1):
        cwd = str(session.cwd) if session.cwd else f"<unknown: {session.project_slug}>"
        suffix = ""
        if session.is_live:
            suffix = f"  {palette.yellow}[running, pid {session.live_pid} - skip unless you want a second view]{palette.reset}"
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
        comment = session.last_prompt or session.first_prompt or session.label
        if shell == "cmd":
            lines.append(f":: {session.label} - {comment}")
            lines.append(f'cd /d "{cwd}"' if cwd else ":: unknown directory")
            lines.append(session.resume_command)
        elif shell == "bash":
            lines.append(f"# {session.label} - {comment}")
            lines.append(f"cd {_quote_sh(cwd)}" if cwd else "# unknown directory")
            lines.append(session.resume_command)
        else:
            lines.append(f"# {session.label} - {comment}")
            lines.append(f"cd {_quote_ps(cwd)}" if cwd else "# unknown directory")
            lines.append(session.resume_command)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_launcher(sessions: Sequence[Session], *, shell: str = "pwsh") -> str:
    """Build a script that opens every session in its own terminal tab."""
    if shell == "cmd":
        head = ["@echo off", "rem generated by Revenant", ""]
        body = [
            # cmd needs double quotes; `!r` emits a single-quoted, backslash-escaped
            # path it cannot cd into. Inside an already-quoted /k argument, a literal
            # double quote is written twice.
            f'start "" cmd /k "cd /d ""{session.cwd}"" && {session.resume_command}"'
            if session.cwd
            else f":: skipped {session.session_id}: unknown directory"
            for session in sessions
        ]
        return "\n".join(head + body) + "\n"

    if shell == "bash":
        head = ["#!/usr/bin/env bash", "# generated by Revenant", "set -euo pipefail", ""]
        body: list[str] = []
        for session in sessions:
            if not session.cwd:
                body.append(f"# skipped {session.session_id}: unknown directory")
                continue
            body.append(f"( cd {_quote_sh(str(session.cwd))} && {session.resume_command} )")
        return "\n".join(head + body) + "\n"

    head = [
        "# generated by Revenant",
        "# Opens every session as a tab in one new Windows Terminal window.",
        "",
        "$ErrorActionPreference = 'Stop'",
        "",
    ]
    usable = [session for session in sessions if session.cwd]
    skipped = [
        f"# skipped {session.session_id}: unknown directory"
        for session in sessions
        if not session.cwd
    ]
    if not usable:
        return "\n".join(head + skipped + ["Write-Host 'Revenant: nothing to restore'"]) + "\n"

    # One wt.exe call with `;`-separated tabs; the semicolons belong to wt, so
    # PowerShell must not eat them - hence the backtick escape.
    parts: list[str] = ["  wt.exe -w new"]
    for index, session in enumerate(usable):
        prefix = "    " if index == 0 else "    `; "
        parts.append(
            f"{prefix}new-tab --title {_quote_ps(session.label)} "
            f"-d {_quote_ps(str(session.cwd))} "
            f"$shell -NoExit -Command {_quote_ps(session.resume_command)}"
        )
    invocation = " `\n".join(parts)

    # wt.exe is a Store alias in an ACL-locked folder; some shells cannot run it.
    fallback = [
        "  if ($LASTEXITCODE -ne 0) { throw 'wt.exe failed' }",
        "} catch {",
        "  Write-Host 'Windows Terminal unavailable - opening one window per session.'",
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


def _shell_exe() -> str:
    return "pwsh" if shutil.which("pwsh") else "powershell"


def build_wt_argv(sessions: Sequence[Session], *, window: str = "new", profile: str | None = None) -> list[str]:
    """Assemble a single `wt.exe` invocation opening one tab per session."""
    argv: list[str] = ["wt.exe", "-w", window]
    shell_exe = _shell_exe()
    first = True
    for session in sessions:
        if not session.cwd:
            continue
        if not first:
            argv.append(";")
        first = False
        argv += ["new-tab", "--title", session.label, "-d", str(session.cwd)]
        if profile:
            argv += ["-p", profile]
        argv += [shell_exe, "-NoExit", "-Command", session.resume_command]
    return argv


def _spawn_separate_windows(sessions: Sequence[Session], *, stream=None) -> int:
    """Fallback path: one console window per session, no Windows Terminal needed.

    `wt.exe` on Windows is a Store app-execution alias inside the ACL-locked
    WindowsApps folder, and some contexts (services, sandboxes, restricted shells)
    are denied execution of it. `start` always works.
    """
    stream = stream if stream is not None else sys.stdout
    shell = _shell_exe()
    opened = 0
    for session in sessions:
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", shell, "-NoExit", "-Command", session.resume_command],
                cwd=str(session.cwd),
                close_fds=True,
            )
            opened += 1
        except OSError as exc:
            print(f"Could not open {session.label}: {exc}", file=stream)
    print(f"Opened {opened} window(s).", file=stream)
    return 0 if opened else 4


def launch(
    sessions: Sequence[Session],
    *,
    window: str = "new",
    profile: str | None = None,
    dry_run: bool = False,
    prefer_tabs: bool = True,
    stream=None,
) -> int:
    """Open the selected sessions: Windows Terminal tabs, or one window each."""
    stream = stream if stream is not None else sys.stdout
    usable = [s for s in sessions if s.cwd]
    if not usable:
        print("Nothing to launch: no session has a known directory.", file=stream)
        return 1

    live = [s for s in usable if s.is_live]
    if live:
        names = ", ".join(f"{s.label}(pid {s.live_pid})" for s in live)
        print(
            f"Refusing to launch {len(live)} still-running session(s): {names}.\n"
            "Two processes on one transcript corrupt it. Close them first, or drop --include-live.",
            file=stream,
        )
        return 2

    argv = build_wt_argv(usable, window=window, profile=profile)
    if dry_run:
        print(" ".join(f'"{part}"' if " " in part else part for part in argv), file=stream)
        return 0

    if os.name != "nt":
        print(
            "Automatic launching is Windows-only. Use --print or --emit revive.sh instead.",
            file=stream,
        )
        return 3

    if prefer_tabs and (shutil.which("wt.exe") or shutil.which("wt")):
        try:
            process = subprocess.Popen(argv, close_fds=True)
        except OSError as exc:
            print(f"Windows Terminal refused to start ({exc}); opening separate windows.", file=stream)
        else:
            try:
                # wt returns immediately on success; a fast non-zero exit means it failed.
                if process.wait(timeout=2) == 0:
                    print(f"Opened {len(usable)} tab(s) in Windows Terminal.", file=stream)
                    return 0
                print("Windows Terminal exited with an error; opening separate windows.", file=stream)
            except subprocess.TimeoutExpired:
                print(f"Opened {len(usable)} tab(s) in Windows Terminal.", file=stream)
                return 0

    return _spawn_separate_windows(usable, stream=stream)


def pick(sessions: Sequence[Session], *, stream=None, reader=input) -> list[Session]:
    """Ask which of the listed sessions to act on. `all`/empty selects everything."""
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
                chosen.extend(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                chosen.append(int(chunk))
            except ValueError:
                continue
    return [sessions[i - 1] for i in dict.fromkeys(chosen) if 1 <= i <= len(sessions)]


def session_to_dict(session: Session) -> dict:
    """Serialise one session - shared by `--json` and the desktop UI."""
    return {
        "sessionId": session.session_id,
        "agent": session.agent.key,
        "cwd": str(session.cwd) if session.cwd else None,
        "label": session.label,
        "lastActive": session.last_active.isoformat() if session.last_active else None,
        "ageLabel": humanize_age(session.last_active),
        "startedAt": session.started_at.isoformat() if session.started_at else None,
        "turns": session.turns,
        "firstPrompt": session.first_prompt,
        "lastPrompt": session.last_prompt,
        "gitBranch": session.git_branch,
        "version": session.version,
        "sizeBytes": session.size_bytes,
        "live": session.is_live,
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
            "  revenant --emit revive.ps1    write a launcher script\n"
            "  revenant snapshot             record what is running right now\n"
            "  revenant gui                  open the desktop app\n"
        ),
    )
    parser.add_argument("command", nargs="?", default="list", choices=["list", "snapshot", "gui"])
    parser.add_argument("--agent", default="claude-code", help="which agent's sessions to look for")
    parser.add_argument("--root", help="agent config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--since", default="24h", help="window start: 24h, 7d, today, all, 2026-09-01")
    parser.add_argument("--until", help="window end (same formats)")
    parser.add_argument("--dir", action="append", default=[], help="only sessions whose path contains this (repeatable)")
    parser.add_argument("--slug", help="only this projects/<slug> directory")
    parser.add_argument("--min-turns", type=int, default=1, help="skip sessions with fewer user prompts (default 1)")
    parser.add_argument("--limit", type=int, default=40, help="max sessions to show (default 40, 0 = no limit)")
    parser.add_argument("--latest-per-dir", action="store_true", help="keep only the newest session per directory")
    parser.add_argument("--include-live", action="store_true", help="also show sessions that are still running")
    parser.add_argument("--only-live", action="store_true", help="show only sessions that are still running")
    parser.add_argument("--from-snapshot", action="store_true", help="restore the set recorded by `revenant snapshot`")

    output = parser.add_argument_group("output")
    output.add_argument("--print", dest="print_cmds", action="store_true", help="print paste-ready cd + resume commands")
    output.add_argument("--emit", metavar="FILE", help="write a launcher script (.ps1 / .sh / .cmd)")
    output.add_argument("--launch", action="store_true", help="open each session in its own terminal tab")
    output.add_argument("--pick", action="store_true", help="choose interactively before acting")
    output.add_argument("--dry-run", action="store_true", help="with --launch, show the command instead of running it")
    output.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    output.add_argument("--shell", choices=["pwsh", "bash", "cmd"], default="pwsh", help="target shell (default pwsh)")
    output.add_argument("--window", default="new", help="wt.exe target window: new, 0, or a name (default new)")
    output.add_argument("--no-tabs", action="store_true", help="open one window per session instead of tabs")
    output.add_argument("--profile", help="Windows Terminal profile for new tabs")
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
    print(f"Snapshot from {captured}: {len(wanted)} session(s) recorded, {len(filtered)} found on disk.\n", file=stream)
    return filtered


def main(argv: Sequence[str] | None = None, *, stream=None) -> int:
    stream = stream if stream is not None else sys.stdout
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    agent = get_agent(args.agent)

    if args.command == "gui":
        from revenant_gui import run_gui  # lazy: the CLI itself stays dependency-free

        return run_gui(agent=agent, root=args.root)

    root = config_root(args.root, agent=agent)
    if not root.is_dir():
        print(f"{agent.label} config dir not found: {root}", file=stream)
        return 1

    if args.command == "snapshot":
        payload = write_snapshot(root, agent=agent)
        count = len(payload["sessions"])
        print(
            f"Recorded {count} running session(s) to {snapshot_path(root, agent=agent)}",
            file=stream,
        )
        for entry in payload["sessions"]:
            print(f"  {entry['name'] or entry['sessionId'][:8]}  {entry['cwd']}", file=stream)
        return 0

    try:
        since = parse_when(args.since)
        until = parse_when(args.until) if args.until else None
    except BadTimeWindow as exc:
        print(f"revenant: {exc}", file=stream)
        return 2
    sessions = scan_sessions(root, since=since, until=until, slug_filter=args.slug, agent=agent)

    if args.from_snapshot:
        # Keep stdout pure when the caller asked for machine-readable output.
        notice = sys.stderr if args.as_json else stream
        sessions = _restrict_to_snapshot(
            sessions, read_snapshot(root, agent=agent), stream=notice
        )

    selected = filter_sessions(
        sessions,
        include_live=args.include_live,
        only_live=args.only_live,
        dirs=args.dir,
        min_turns=args.min_turns,
        latest_per_dir=args.latest_per_dir,
        limit=args.limit or None,
    )

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
        print(render_commands(selected, shell=args.shell), file=stream)

    if args.emit:
        target = Path(args.emit).expanduser()
        suffix = target.suffix.lower()
        shell = {"ps1": "pwsh", "sh": "bash", "bash": "bash", "cmd": "cmd", "bat": "cmd"}.get(
            suffix.lstrip("."), args.shell
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_launcher(selected, shell=shell), encoding="utf-8")
        print(f"\nWrote {target} ({len(selected)} session(s)). Run it to bring them back.", file=stream)

    if args.launch:
        return launch(
            selected,
            window=args.window,
            profile=args.profile,
            dry_run=args.dry_run,
            prefer_tabs=not args.no_tabs,
            stream=stream,
        )

    if not acting:
        print(
            "\nNext: --print (commands) · --emit revive.ps1 (script) · --launch (open tabs) · --pick (choose first)",
            file=stream,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
