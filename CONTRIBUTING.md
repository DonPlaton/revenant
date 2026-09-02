# Contributing

Small project, simple rules.

## Running it

```bash
git clone https://github.com/DonPlaton/revenant
cd revenant
python -m pytest tests -q     # 159 tests, no network, no real session touched
python revenant.py --since 7d # the command line
python revenant_gui.py        # the desktop app
```

## Layout

| file | what lives there |
|---|---|
| `agents.py` | where each agent keeps its transcripts, how to read them, how to resume one |
| `terminals.py` | every terminal backend, as pure argv builders |
| `revenant.py` | discovery, filtering, rendering, the command line |
| `revenant_gui.py` | the local HTTP backend behind the desktop window |
| `ui/index.html` | the whole interface, one file, no build step |

`revenant.py`, `agents.py` and `terminals.py` import nothing outside the standard library and must
stay that way. The point of this tool is that it still runs on a machine you have only just
rebooted. Only the desktop window may use `pywebview`, and it falls back to a browser window when
that is missing.

## Adding an agent

Subclass `Agent` in `agents.py` and add it to the `AGENTS` registry. You need to describe:

- where the config directory lives, and which environment variable overrides it
- a glob for transcript files, and how to read a session id out of a path
- `head()`, which pulls the working directory and version out of the first records
- `tail()`, which recovers the first and last prompts from the end of a file
- `history()`, if the agent keeps a prompt index
- `live_registry()`, if the agent records which sessions are running, or `live_window` seconds of
  recent activity to treat as possibly open when it does not
- the resume command template, and the process names a live session runs under

Open an issue first so we can agree on the shape. Bring a real transcript, redacted.

## Adding a terminal

Subclass `Terminal` in `terminals.py`, declare which platforms it runs on, implement `available()`
and `plan()`, and add it to `ORDER` and `ALL`. A `plan()` builds argv lists and runs nothing, so
your backend gets tested on every platform even though only one can execute it.

## Rules that are not negotiable

- Never write to, signal, or kill a session that belongs to an agent.
- A session that may still be running is excluded by default and can never be launched while it
  runs, because two processes on one transcript corrupt it.
- Every change to discovery or safety comes with a test.

## Style

Type hints, docstrings that say why rather than what, no comments restating the code. Conventional
commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `perf:`).
