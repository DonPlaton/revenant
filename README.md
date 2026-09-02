<div align="center">

<img src="assets/social-card.png" alt="Revenant, bring your agent sessions back from the dead" width="820">

<br>

[![tests](https://github.com/DonPlaton/revenant/actions/workflows/tests.yml/badge.svg)](https://github.com/DonPlaton/revenant/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-dd7b52.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-dd7b52.svg)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-none-8aa878.svg)](pyproject.toml)

**Your machine crashed with nine agent sessions open. Get them all back in one click.**

</div>

---

## The problem

Claude Code's `--resume` picker only lists conversations from the directory you are standing in.
That is fine for one project. It is useless when a bluescreen takes down nine sessions spread
across nine directories, because now you have to remember every path, `cd` there, and hunt for
the right conversation in each picker. Codex does not even have a picker that spans directories.

Revenant lists every session that was active in a window you choose, across every directory and
both agents, and brings back the ones you pick.

<div align="center">

<img src="assets/demo.gif" alt="Dragging the time window from six hours to ninety days while the register refills" width="760">

<sub>Drag the caret from six hours to ninety days. Sessions hang off the time axis, newest first.
A session that is still running is hatched and held back. Then hit REVIVE.</sub>

</div>

## Install

### As a desktop app

```powershell
git clone https://github.com/DonPlaton/revenant
cd revenant
.\install.ps1 -NativeWindow        # Windows
```

```bash
git clone https://github.com/DonPlaton/revenant
cd revenant
./install.sh --native-window       # macOS and Linux
```

On Windows you get Desktop and Start Menu shortcuts. On macOS you get `~/Applications/Revenant.app`
with a real icon. On Linux you get a `.desktop` entry in your application menu. Nothing is copied
out of the folder and no PATH is changed. `--native-window` also installs
[pywebview](https://pywebview.flowrl.com/) so the app opens in its own frameless window; without
it the app opens as a chromeless Chrome or Edge window instead. The uninstall scripts remove what
the installers added.

### As a command line tool

```bash
pip install -e .          # then: revenant --since 7d
```

Or just run the file. It imports nothing outside the standard library, which matters on a machine
you have only just rebooted:

```bash
python revenant.py --since 7d
```

## Using it

The app has two controls: a caret for how far back to look, and REVIVE. Click a row to mark or
unmark it, double click it (or press `O` with it focused) to open its folder. `Enter` revives what
is marked, `Ctrl+R` rescans, `Esc` closes. Everything is reachable from the keyboard.

The command line does the same and more:

```bash
revenant                        # what was alive in the last 24 hours
revenant --since 7d --pick      # choose from the last week: 1,3,5 or 2-4 or all
revenant --since 6h --launch    # reopen each in its own terminal tab
revenant --all-agents           # every agent installed on this machine
revenant --print                # paste-ready cd and resume command pairs
revenant --emit revive.sh       # a launcher script you can rerun any time
revenant gui                    # open the desktop app
revenant agents                 # what is installed, and where it keeps things
revenant terminals              # where sessions can open here
```

<details>
<summary><b>All flags</b></summary>

Choosing sessions:

| flag | effect |
|---|---|
| `--since 24h` | window start: `30s`, `90m`, `24h`, `7d`, `2w`, `today`, `all`, `2026-09-01`, `2026-09-01T10:30` (default `24h`) |
| `--until <time>` | window end, same formats |
| `--agent <key>` | `claude-code` or `codex` |
| `--all-agents` | scan every agent installed here and merge the results |
| `--dir <text>` | only sessions whose path contains this, repeatable |
| `--slug <text>` | only one transcript folder |
| `--latest-per-dir` | keep just the newest session per directory |
| `--min-turns N` | skip sessions with fewer real prompts (default `1`, so `/model`-only sessions vanish) |
| `--limit N` | cap the list (default `40`, `0` for no limit) |
| `--include-live` / `--only-live` | show sessions that may still be running |
| `--from-snapshot` | restore exactly the set recorded by `revenant snapshot` |
| `--root <path>` | read a config directory somewhere else |

Acting on them:

| flag | effect |
|---|---|
| *(none)* | print the table |
| `--print` | `cd` and resume pairs, ready to paste |
| `--emit FILE` | write a launcher script; `.ps1`, `.sh` and `.cmd` pick their own syntax |
| `--launch` | open the sessions now |
| `--terminal <key>` | where to open them, from `revenant terminals` |
| `--pick` | choose interactively before acting |
| `--dry-run` | with `--launch`, print the commands instead of running them |
| `--json` | machine-readable output |
| `--window`, `--profile` | target a specific Windows Terminal window or profile |

</details>

## Where sessions reopen

Revenant picks the best terminal it can find, and `--terminal` overrides it. Being inside tmux
wins over everything, which is what you want over SSH.

| platform | tabs in one window | one window each |
|---|---|---|
| Windows | Windows Terminal, tmux | the console |
| macOS | iTerm2, tmux | Terminal.app, kitty, WezTerm, Ghostty, Alacritty |
| Linux | GNOME Terminal, Konsole, Xfce Terminal, tmux | kitty, WezTerm, Ghostty, Alacritty, foot, xterm |

Windows Terminal can be installed, look available, and still refuse to run, because it is a Store
alias inside a folder some shells are denied access to. Revenant notices and moves to the next
terminal on the list rather than failing.

## Agents

| agent | transcripts | how a live session is spotted |
|---|---|---|
| Claude Code | `~/.claude/projects/<slug>/<uuid>.jsonl` | it registers itself, so this is exact |
| Codex | `~/.codex/sessions/<date>/rollout-*.jsonl` | no registry, so anything touched in the last two minutes is held back |

Sessions you start from the integrated terminal in VS Code, Cursor or Windsurf are ordinary CLI
sessions, so they are found and revived like any other. Chat panels built into those editors keep
their history in the editor's own database and expose no way to resume one, so Revenant does not
pretend to handle them.

Adding another agent means one subclass in `agents.py` and one line in the registry. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## How it works

<div align="center">
<img src="assets/how-it-works.png" alt="Four steps: the machine dies, transcripts outlive the crash, you pick a window, revive" width="960">
</div>

The registry of running sessions is pruned when the agent next starts, so after a crash it is
empty. That is why tools built on a snapshot daemon lose everything when the daemon was not
running. Revenant reads the transcripts, which are always there, and uses the registry only to
work out what is alive right now.

## What it costs

Measured on 34 transcripts totalling 534 MB, on an eight core desktop:

| | |
|---|---|
| scan a seven day window | 40 ms |
| scan everything on disk | 50 ms |
| repeat request in the app | 0.2 ms, served from an eight second cache |
| peak Python heap for a full scan | 1.5 MB |
| the app while you look at it | 0.3% of one core |

Liveness used to cost half a second because it shelled out to `tasklist` and walked all 428
processes on the machine. It now asks the kernel about the handful of process ids in the registry,
which takes 0.2 ms.

The desktop window is a WebView2 or WebKit surface, so it holds around 430 MB while it is open,
the same as any browser-backed app. It is meant to be opened, used for ten seconds and closed, and
it takes its processes with it. Nothing stays resident: no tray icon, no service, no autostart. If
you want the light path, the command line does the same work in 40 ms and about 15 MB.

## Safety

Revenant is read only with respect to your agents. It never writes to, signals, or kills a
session, and a test asserts that a full run leaves every file under `~/.claude` unchanged to the
byte.

A session whose process is alive is held back, and `--launch` refuses it outright, because two
processes writing one transcript corrupt it. On Windows the check compares the process id against
the real executable name, so a recycled id cannot pose as a live session. Codex keeps no registry,
so a rollout file touched in the last two minutes is treated as possibly open.

The desktop backend binds to `127.0.0.1` on an ephemeral port, mints a token at startup and
requires it on every request, rejects any request whose `Host` header is not that exact address,
refuses path traversal out of `ui/`, and stops when the window goes quiet. The interface loads no
remote fonts, scripts or styles, so it works with the network off.

## How it compares

Several session restore tools exist for Claude Code. They are macOS or tmux first, and all of
them are terminal only:

| | platform | interface | agents |
|---|---|---|---|
| **Revenant** | **Windows, macOS, Linux** | **desktop app** and CLI | **Claude Code, Codex** |
| [Supersynergy/claude-session-restore](https://github.com/Supersynergy/claude-session-restore) | macOS, some Linux | CLI and MCP | Claude Code |
| [Livshitz/claude-revive](https://github.com/Livshitz/claude-revive) | macOS | TUI picker | Claude Code |
| [drewburchfield/claude-session-manager](https://github.com/drewburchfield/claude-session-manager) | macOS | CLI, snapshots | Claude Code |
| [asadtariq96/cc-session-restore](https://github.com/asadtariq96/cc-session-restore) | macOS with iTerm2 | CLI and a LaunchAgent | Claude Code |
| [Mahrkeenerh/ClaudeRestore](https://github.com/Mahrkeenerh/ClaudeRestore) | Linux | CLI | Claude Code |
| [cookiecad/tmux-claude-resurrect](https://github.com/cookiecad/tmux-claude-resurrect) | tmux | plugin | Claude Code |
| [STRML/cmux-restore](https://github.com/STRML/cmux-restore) | cmux | CLI | Claude Code |

If you live in tmux, the tmux plugin is a better fit. Revenant is for people who run several
agents at once, want a window rather than a flag, and are not necessarily on a Mac.

## Development

```bash
python -m pytest tests -q
```

160 tests, no network, no real session touched. Everything runs against a synthetic config
directory in `tmp_path`. They cover both agents' file formats, live process detection and id
reuse, the refusal to relaunch a running session, the argv of all fourteen terminal backends on
all three platforms, quoting of paths with spaces and apostrophes, corrupt and truncated
transcripts, the desktop backend's token, host and traversal guards, and the read only guarantee.

Regenerate the images after changing the interface:

```bash
python assets/src/build.py
```

The demo recording runs against a synthetic config directory, so no real path or prompt ends up in
the repository.

## Design

The interface was drawn as a
[design canvas](https://claude.ai/code/artifact/8880a686-799e-4516-991a-f074c1511c59) before it was
built: sessions as marks on a time axis rather than a stack of cards, one decisive action, and
motion that reads as mechanical rather than decorative. The canvas uses Spectral and IBM Plex Mono.
The app ships with metrically similar system faces instead, so it never asks the network for a
font.

## License

MIT. See [LICENSE](LICENSE).
