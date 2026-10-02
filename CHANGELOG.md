# Changelog

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
