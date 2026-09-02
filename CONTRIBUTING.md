# Contributing

Small project, simple rules.

## Running it

```bash
git clone https://github.com/DonPlaton/revenant
cd revenant
python -m pytest tests -q     # 102 tests, no network, no real sessions touched
python revenant.py --since 7d # the CLI
python revenant_gui.py        # the desktop app
```

The CLI (`revenant.py`) has **zero dependencies** and must stay that way — the whole point is
that it still runs on a machine you just rebooted. Only the desktop window may use `pywebview`,
and it degrades to a browser window when that is missing.

## Adding another agent

`revenant.py` has an `Agent` dataclass and an `AGENTS` registry. An agent that keeps one
transcript file per conversation on disk needs a new entry with its config directory, env var,
resume command template, and the process images a live session runs under. If its transcripts
have a different shape, `_head_metadata` and `_tail_prompts` are the two functions to
generalise. Please open an issue first so we can agree on the shape.

## Rules that are not negotiable

- Never write to, signal, or kill a session that belongs to the agent.
- A session with a live process is excluded by default and can never be launched again while
  it runs — two processes on one transcript corrupt it.
- Every change that touches discovery or safety comes with a test.

## Style

Type hints, docstrings that say *why*, no comments restating the code. Conventional commits
(`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
