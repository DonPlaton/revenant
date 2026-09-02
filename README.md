<div align="center">

<img src="assets/social-card.png" alt="Revenant — bring your agent sessions back from the dead" width="820">

<br>

[![tests](https://github.com/DonPlaton/revenant/actions/workflows/tests.yml/badge.svg)](https://github.com/DonPlaton/revenant/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-d97757.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-d97757.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-none-83b069.svg)](pyproject.toml)

**Your machine crashed with nine agent sessions open. Get them all back in one click.**

</div>

---

## The problem

Claude Code's own `--resume` picker only lists conversations from **the directory you are
standing in**. That is fine for one project. It is useless when a bluescreen takes down nine
sessions spread across nine directories — now you have to remember every path, `cd` there, and
hunt for the right conversation in each picker.

Revenant lists every session that was active in a window you choose, **across all
directories**, and brings the ones you pick back to life.

<div align="center">

<img src="assets/demo.gif" alt="Dragging the time window from 6 hours to 90 days; the session list refilters live" width="760">

<sub>Drag the window from 6 hours to 90 days — the list refilters live. Running sessions are
marked and locked. Then hit REVIVE.</sub>

</div>

## Install

### As a desktop app (Windows)

```powershell
git clone https://github.com/DonPlaton/revenant
cd revenant
.\install.ps1 -NativeWindow
```

Creates Desktop and Start Menu shortcuts with the app icon. `-NativeWindow` also installs
[`pywebview`](https://pywebview.flowrl.com/) so the app opens in a real frameless window;
without it, it opens as a chromeless Chrome/Edge window instead. Nothing is copied anywhere
and `PATH` is not touched — `.\uninstall.ps1` removes the shortcuts.

### As a CLI (anywhere)

```bash
pip install -e .        # then: revenant --since 7d
```

or just run the file — it has no dependencies at all:

```bash
python revenant.py --since 7d
```

## Using it

The app has exactly two controls: **a slider for how far back to look**, and **REVIVE**.
Click a card to include or exclude it, click a path to open that folder.
`Enter` revives, `Ctrl+R` rescans, `Esc` closes.

The CLI does the same and more:

```powershell
revenant                       # what was alive in the last 24h
revenant --since 7d --pick     # choose from the last week: 1,3,5 or 2-4 or all
revenant --since 6h --launch   # reopen each in its own terminal tab
revenant --since 6h --print    # paste-ready `cd` + `claude --resume` pairs
revenant --emit revive.ps1     # a launcher script you can rerun any time
revenant gui                   # open the desktop app
revenant snapshot              # record exactly what is running right now
```

<details>
<summary><b>All flags</b></summary>

**Choosing sessions**

| flag | effect |
|---|---|
| `--since 24h` | window start: `30s`, `90m`, `24h`, `7d`, `2w`, `today`, `all`, `2026-09-01`, `2026-09-01T10:30` (default `24h`) |
| `--until <time>` | window end, same formats |
| `--dir <text>` | only sessions whose path contains this; repeatable |
| `--slug <text>` | only one `projects/<slug>` directory |
| `--latest-per-dir` | keep just the newest session per directory |
| `--min-turns N` | skip sessions with fewer real prompts (default `1`, so `/model`-only sessions vanish) |
| `--limit N` | cap the list (default `40`, `0` = no limit) |
| `--include-live` / `--only-live` | show sessions that are still running |
| `--from-snapshot` | restore exactly the set recorded by `revenant snapshot` |
| `--agent claude-code` | which agent's sessions to look for |
| `--root <path>` | agent config dir (default `$CLAUDE_CONFIG_DIR` or `~/.claude`) |

**Acting on them**

| flag | effect |
|---|---|
| *(none)* | print the table |
| `--print` | `cd` + resume pairs, ready to paste |
| `--emit FILE` | write a launcher script; `.ps1`, `.sh`, `.cmd` pick their own syntax |
| `--launch` | open the sessions immediately |
| `--pick` | choose interactively before acting |
| `--dry-run` | with `--launch`, show the command instead of running it |
| `--json` | machine-readable output |
| `--no-tabs` | one window per session instead of Windows Terminal tabs |
| `--window`, `--profile` | target a specific `wt` window / profile |

</details>

## How it works

<div align="center">
<img src="assets/how-it-works.png" alt="Four steps: the machine dies, transcripts outlive the crash, you pick a window, revive" width="960">
</div>

The registry of running sessions is pruned when the agent next starts, so after a crash it is
empty — which is exactly why tools that rely on a snapshot daemon lose everything if the daemon
was not running. Revenant reads the **transcripts**, which are always there, and uses the
registry only to detect what is *currently* alive.

## Safety

Revenant is read-only with respect to your agent. It never writes to, signals, or kills a
session — there is a test asserting that a full run leaves every file under `~/.claude`
byte-for-byte unchanged.

Sessions whose process is still alive are detected (registry cross-checked against the live
process table, with an image-name check so a recycled PID cannot masquerade as a live session)
and **excluded by default**. `--launch` refuses them outright even with `--include-live`,
because two processes writing one transcript corrupt it.

The desktop app's backend binds to `127.0.0.1` on an ephemeral port, requires a token minted at
startup on every request, rejects any request whose `Host` header is not that exact
`127.0.0.1:port` (so a rebinding page cannot drive it), refuses path traversal out of `ui/`,
and stops when the window closes. Nothing leaves your machine; the UI loads no remote fonts,
scripts or styles.

## How it compares

There are several session-restore tools for Claude Code. Almost all of them are macOS- or
tmux-first, and all of them are terminal-only:

| | platform | interface | discovery |
|---|---|---|---|
| **Revenant** | **Windows** (CLI everywhere) | **desktop app** + CLI | transcripts |
| [Supersynergy/claude-session-restore](https://github.com/Supersynergy/claude-session-restore) | macOS, some Linux | CLI + MCP | transcripts |
| [Livshitz/claude-revive](https://github.com/Livshitz/claude-revive) | macOS | TUI picker | transcripts |
| [drewburchfield/claude-session-manager](https://github.com/drewburchfield/claude-session-manager) | macOS | CLI | snapshots |
| [asadtariq96/cc-session-restore](https://github.com/asadtariq96/cc-session-restore) | macOS (iTerm2) | CLI + LaunchAgent | 60s snapshots |
| [Mahrkeenerh/ClaudeRestore](https://github.com/Mahrkeenerh/ClaudeRestore) | Linux | CLI | transcripts |
| [cookiecad/tmux-claude-resurrect](https://github.com/cookiecad/tmux-claude-resurrect) | tmux | plugin | tmux-resurrect |
| [STRML/cmux-restore](https://github.com/STRML/cmux-restore) | cmux | CLI | cmux state |

If you are on macOS with iTerm2 or tmux, those tools are a better fit. Revenant is for the
Windows Terminal + PowerShell workflow nobody had covered, and for people who would rather
drag a slider than remember flags.

## Supported agents

Claude Code today. The `Agent` dataclass in `revenant.py` is the seam — any agent that keeps
one transcript file per conversation can be added by describing its config directory, resume
command and process names. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
python -m pytest tests -q
```

102 tests, no network, no real sessions touched — everything runs against a synthetic config
root in `tmp_path`. They cover live-process detection and PID reuse, the refusal to relaunch a
running session, quoting of paths containing apostrophes, corrupt and truncated transcripts,
the desktop backend's token/host/traversal guards, and the read-only guarantee.

Regenerate the images after changing the UI:

```bash
python assets/src/build.py     # icon, social card, infographic, demo gif
```

## License

MIT — see [LICENSE](LICENSE).
