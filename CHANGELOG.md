# Changelog

## 1.7.1

- Put session titles first for every agent, including held sessions, and use the first
  prompt when no title was saved. Prefer named Codex index entries to generated titles.
- Add a copy-path button to every known folder, including held sessions, and allow
  selecting path text. Clipboard failures now report an error instead of success.
- Launch OpenCode directly in Windows terminals, resolving the native npm executable
  before falling back to a cmd shim, so its TUI receives the terminal without PowerShell.

## 1.7.0

- Add OpenCode session discovery and revival across all project directories, including child
  and archived sessions, SQLite WAL data, and the legacy JSON storage format.
- Read OpenCode activity per session so a recently written shared database does not mark every
  conversation as live. Recheck that activity before opening a terminal.
- Discover Codex rollouts recursively, including archives and imported locations recorded in
  the state database. Recover current titles, working directories and branch metadata.
- Read Codex's projected desktop history and retain sessions with a stored first user message
  when a bounded transcript search cannot find their prompts.
- Keep the actual user text in multipart Codex prompts that also contain injected instructions,
  and count repeated user turns without counting duplicated event/response pairs.
- Open the desktop app on all detected agents by default; keep explicit agent and root choices.
- Show OpenCode's own colour and resume command in the interface and revival animation.
