#!/usr/bin/env python3
"""The agents Revenant knows how to find, read and resume.

Each agent keeps one transcript file per conversation somewhere under a config
directory. An `Agent` describes where that is, how to pull the working directory
and prompts out of a transcript, how to tell which conversations are still
running, and what command brings one back.

Adding an agent means one subclass and one line in `AGENTS`.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# Records at the head of a transcript that carry cwd, version and branch.
HEAD_LINES = 80
# Bytes read from the end of a transcript when the prompt index has no entry for it.
TAIL_BYTES = 256 * 1024

#: Claude Code appends the session's name to the transcript whenever it changes,
#: as one record per kind. Highest priority first: a name the user set with
#: `/rename` beats a title the model generated, which beats the agent's own name.
TITLE_RECORDS = (("custom-title", "customTitle"), ("ai-title", "aiTitle"), ("agent-name", "agentName"))

# Harness plumbing that lands in the transcript looking like something the user typed.
_META_PROMPT = re.compile(
    r"^\s*<(local-command-caveat|local-command-stdout|command-name|command-message"
    r"|task-notification|system-reminder|user-prompt-submit-hook)"
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def clean_prompt(text: object, *, limit: int = 160) -> str:
    """Flatten a prompt to one printable line."""
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def is_meaningful(prompt: str) -> bool:
    """True for prompts a person actually typed, not slash commands or harness noise."""
    if not prompt or _META_PROMPT.match(prompt):
        return False
    return not prompt.startswith("/")


def from_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def from_epoch(value: object) -> datetime | None:
    """Read a timestamp that may be in seconds or milliseconds."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number > 1e11:  # milliseconds
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _records(path: Path, *, limit: int | None = None) -> Iterator[dict]:
    """Yield JSON objects from a JSONL file, skipping anything unreadable."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for index, line in enumerate(handle):
            if limit is not None and index >= limit:
                return
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _tail_lines(path: Path, *, window: int) -> Iterator[bytes]:
    """Yield whole lines from the last `window` bytes of a file, as raw bytes.

    Callers that only want a few kinds of record can test the bytes and skip the
    cost of parsing the rest.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > window:
                handle.seek(size - window)
                handle.readline()  # drop the partial line
            blob = handle.read()
    except OSError:
        return
    yield from blob.splitlines()


def _tail_records(path: Path, *, window: int) -> Iterator[dict]:
    """Yield JSON objects from the last `window` bytes of a JSONL file."""
    for line in _tail_lines(path, window=window):
        try:
            record = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


class Agent:
    """One coding agent. Subclasses fill in the file formats."""

    key = "agent"
    label = "Agent"
    env_var = ""
    resume_template = "{id}"
    #: Where a config directory lives when the environment variable is unset.
    home_relative = ".agent"
    #: Glob for transcripts, relative to the config directory.
    transcript_glob = "*.jsonl"
    #: Seconds of recent activity that count as "may still be running" for agents
    #: that keep no registry of live sessions. Zero means the registry is the only
    #: signal.
    live_window = 0.0
    #: Process images a live session can run under, guarding against PID reuse.
    process_images = frozenset({"node", "node.exe"})
    #: One line explaining how liveness is decided, shown in the UI.
    liveness_note = ""

    def __init__(self, *, process_images: Iterable[str] | None = None) -> None:
        if process_images is not None:
            self.process_images = frozenset(process_images)

    # -- identity --------------------------------------------------------- #

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.key}>"

    def variant(self, **overrides) -> Agent:
        """A copy with attributes replaced. Used by tests and the demo recorder."""
        clone = type(self)()
        for name, value in overrides.items():
            if name == "process_images":
                value = frozenset(value)
            setattr(clone, name, value)
        return clone

    def config_dir(self, explicit: str | os.PathLike[str] | None = None) -> Path:
        if explicit:
            return Path(explicit).expanduser()
        env = os.environ.get(self.env_var) if self.env_var else None
        return Path(env).expanduser() if env else Path.home() / self.home_relative

    def resume_command(self, session_id: str) -> str:
        return self.resume_template.format(id=session_id)

    def installed(self) -> bool:
        return self.config_dir().is_dir()

    # -- discovery -------------------------------------------------------- #

    def transcripts(self, root: Path) -> Iterator[Path]:
        yield from root.glob(self.transcript_glob)

    def session_id(self, transcript: Path) -> str:
        return transcript.stem

    def root_of(self, transcript: Path) -> Path:
        """The config directory this transcript was found under.

        Derived from the glob's depth so that it stays right for `--root` and for
        any agent added later.
        """
        return transcript.parents[self.transcript_glob.count("/")]

    def group(self, transcript: Path) -> str:
        """A short label for where the transcript is filed, used as a fallback name."""
        return transcript.parent.name

    def head(self, transcript: Path) -> dict:
        """Working directory, version, branch and start time from the first records."""
        raise NotImplementedError

    def tail(self, transcript: Path) -> tuple[str, str, int, bool]:
        """(first, last, count, complete) prompts, read from the end of the file."""
        raise NotImplementedError

    def history(self, root: Path) -> dict[str, list[tuple[datetime, str, str]]]:
        """sessionId -> [(when, prompt, project)] from the agent's prompt index."""
        return {}

    def title(self, transcript: Path) -> str:
        """The session's own name, read from its transcript. Empty when it has none."""
        return ""

    def titles(self, root: Path) -> dict[str, str]:
        """sessionId -> name, for agents that keep names in one index file."""
        return {}

    def live_registry(self, root: Path) -> dict[str, dict]:
        """sessionId -> {pid, cwd, name, status} for sessions the agent says are running."""
        return {}


class ClaudeCode(Agent):
    """Anthropic's Claude Code CLI.

    Transcripts live at `projects/<slug>/<uuid>.jsonl`; running sessions are
    registered in `sessions/<pid>.json` and that registry is pruned on startup, so
    after a crash it is empty and the transcripts are all that is left.
    """

    key = "claude-code"
    label = "Claude Code"
    env_var = "CLAUDE_CONFIG_DIR"
    home_relative = ".claude"
    transcript_glob = "projects/*/*.jsonl"
    resume_template = "claude --resume {id}"
    process_images = frozenset({"claude.exe", "claude", "node.exe", "node", "bun.exe", "bun"})
    liveness_note = "running sessions register themselves, so this is exact"

    def head(self, transcript: Path) -> dict:
        meta: dict = {}
        for index, record in enumerate(_records(transcript, limit=HEAD_LINES * 8)):
            if index >= HEAD_LINES and "cwd" in meta:
                break
            for key in ("cwd", "version", "gitBranch"):
                if key not in meta and record.get(key):
                    meta[key] = record[key]
            if "started" not in meta:
                started = from_iso(record.get("timestamp"))
                if started:
                    meta["started"] = started
        return meta

    def tail(self, transcript: Path) -> tuple[str, str, int, bool]:
        complete = True
        try:
            complete = transcript.stat().st_size <= TAIL_BYTES
        except OSError:
            return "", "", 0, False

        prompts: list[str] = []
        for record in _tail_records(transcript, window=TAIL_BYTES):
            if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            text = clean_prompt(content)
            if is_meaningful(text):
                prompts.append(text)
        if not prompts:
            return "", "", 0, complete
        return (prompts[0] if complete else ""), prompts[-1], len(prompts), complete

    def title(self, transcript: Path) -> str:
        """The name shown in Claude Code's own session picker.

        Naming records are rewritten on almost every turn, so the last one in the
        file is current. Candidate lines are picked out with a substring test first,
        because parsing every record in the window would cost more than the scan.
        """
        kinds = [kind.encode() for kind, _ in TITLE_RECORDS]
        newest: dict[str, str] = {}
        for raw in _tail_lines(transcript, window=TAIL_BYTES):
            if not any(kind in raw for kind in kinds):
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            for _, field in TITLE_RECORDS:
                value = record.get(field)
                if isinstance(value, str) and value.strip():
                    newest[field] = value.strip()
        for _, field in TITLE_RECORDS:
            if field in newest:
                return clean_prompt(newest[field], limit=80)
        return ""

    def history(self, root: Path) -> dict[str, list[tuple[datetime, str, str]]]:
        index: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
        for record in _records(root / "history.jsonl"):
            session_id = record.get("sessionId")
            when = from_epoch(record.get("timestamp"))
            if session_id and when:
                index[session_id].append(
                    (when, clean_prompt(record.get("display")), record.get("project") or "")
                )
        for entries in index.values():
            entries.sort(key=lambda item: item[0])
        return index

    def live_registry(self, root: Path) -> dict[str, dict]:
        directory = root / "sessions"
        if not directory.is_dir():
            return {}
        found: dict[str, dict] = {}
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("sessionId") and record.get("pid"):
                found[record["sessionId"]] = record
        return found


class Codex(Agent):
    """OpenAI's Codex CLI.

    Transcripts live at `sessions/<year>/<month>/<day>/rollout-<stamp>-<uuid>.jsonl`.
    Codex keeps no registry of running sessions, so a conversation that was written
    to moments ago is treated as possibly still open.
    """

    key = "codex"
    label = "Codex"
    env_var = "CODEX_HOME"
    home_relative = ".codex"
    transcript_glob = "sessions/*/*/*/rollout-*.jsonl"
    resume_template = "codex resume {id}"
    live_window = 120.0
    process_images = frozenset({"codex.exe", "codex", "node.exe", "node"})
    liveness_note = "Codex keeps no registry, so anything touched in the last 2 minutes is held back"

    def session_id(self, transcript: Path) -> str:
        match = _UUID.search(transcript.stem)
        return match.group(0) if match else transcript.stem

    def group(self, transcript: Path) -> str:
        return "codex"

    def head(self, transcript: Path) -> dict:
        meta: dict = {}
        # session_meta is the first record, and it carries a large instruction blob,
        # so reading further would cost real time for nothing.
        for record in _records(transcript, limit=4):
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload") or {}
            if payload.get("cwd"):
                meta["cwd"] = payload["cwd"]
            if payload.get("cli_version"):
                meta["version"] = payload["cli_version"]
            started = from_iso(payload.get("timestamp")) or from_iso(record.get("timestamp"))
            if started:
                meta["started"] = started
            break
        return meta

    @staticmethod
    def _prompt_of(record: dict) -> str:
        """The text a person typed, or "" for anything else in the rollout."""
        payload = record.get("payload") or {}
        if record.get("type") == "event_msg" and payload.get("type") == "user_message":
            return clean_prompt(payload.get("message") or payload.get("text"))
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            content = payload.get("content")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            text = clean_prompt(content)
            # Codex replays the AGENTS.md instructions as the first user message.
            if text.startswith("# AGENTS.md instructions"):
                return ""
            return text
        return ""

    def tail(self, transcript: Path) -> tuple[str, str, int, bool]:
        complete = True
        try:
            complete = transcript.stat().st_size <= TAIL_BYTES
        except OSError:
            return "", "", 0, False

        prompts = [
            text
            for record in _tail_records(transcript, window=TAIL_BYTES)
            if is_meaningful(text := self._prompt_of(record))
        ]
        if not prompts:
            return "", "", 0, complete
        return (prompts[0] if complete else ""), prompts[-1], len(prompts), complete

    def history(self, root: Path) -> dict[str, list[tuple[datetime, str, str]]]:
        index: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
        for record in _records(root / "history.jsonl"):
            session_id = record.get("session_id")
            when = from_epoch(record.get("ts"))
            if session_id and when:
                index[session_id].append((when, clean_prompt(record.get("text")), ""))
        for entries in index.values():
            entries.sort(key=lambda item: item[0])
        return index


    def titles(self, root: Path) -> dict[str, str]:
        """Codex names threads in one index file rather than in the rollouts."""
        found: dict[str, str] = {}
        for record in _records(root / "session_index.jsonl"):
            name = record.get("thread_name")
            if record.get("id") and isinstance(name, str) and name.strip():
                found[record["id"]] = clean_prompt(name, limit=80)
        return found


AGENTS: dict[str, Agent] = {agent.key: agent for agent in (ClaudeCode(), Codex())}
DEFAULT_AGENT = AGENTS["claude-code"]


def get_agent(key: str | None) -> Agent:
    if not key:
        return DEFAULT_AGENT
    try:
        return AGENTS[key]
    except KeyError:
        known = ", ".join(sorted(AGENTS))
        raise SystemExit(f"Unknown agent {key!r}. Known: {known}") from None


def installed_agents() -> list[Agent]:
    """Agents with a config directory on this machine, most useful first."""
    return [agent for agent in AGENTS.values() if agent.installed()]
