# Changelog

The benchmark history behind each decision is in the README.

## 0.1.1 — 2026-10-30

- Main can finish delegated or read-only work without the one-time prompt to edit its own workspace. Ordinary work sessions and comparison candidates keep the existing no-edit confirmation guard.
- Clarify the bounded demo prompt to wait for child checks, instead of spending its step budget on immediate progress polls.
- Record browser validation of delegation, independent checks, comparison, Apply, and session reopen, with both earlier unsuccessful coordination attempts retained.

## 0.1.0 — 2026-10-30

### Front ends
- Persistent Main and named work sessions, independent workers, saved history, and reopen/restart recovery.
- Before / After check evidence and two-candidate comparison with separate Choose and Apply actions.
- `prax` opens a web interface on localhost: chat with tool calls, changes with per-file apply, checks, to-dos and rounds in a sidebar, a slash-command menu, Stop.
- `prax ui`, a full-screen terminal interface, and `prax session`, a plain prompt for pipes and scripts, on the same session runtime: `/diff /apply /undo /goal /resume /effort /verify /stop /status`.
- `prax run`, `goal`, `resume`, `init`, `status`, `inspect`, `report`, `config`, `prompt`, `doctor`, `version`.
- The web interface serves its page and state only with the session token from the printed URL, and refuses requests whose Host header names anything but 127.0.0.1 or localhost on its port.

### How work is done
- Every command and edit is a checkpoint in an immutable sandbox image; `restore` cannot discard every repository edit.
- Test and lint commands run by the harness after each round, independently of the model, with their results tied to the workspace image they ran on.
- Those checks run in a fork of a clean image that holds exactly the diff an apply would write, so scratch files and packages the agent installed cannot make them pass. The same checks run without the diff in a parallel fork, and a failure is reported as new with the change or as failing without it too; the model is told the same during its own test runs.
- Goal runs in rounds until the configured check passes or the model reports the goal met.
- Applying is journaled and recoverable: an interrupted apply finishes on the next `/apply` or on resuming the run's last checkpoint.
- Files that look like reproduction scripts and binary files the run created stay in the diff but are left out of an apply unless named.
- Context management: tool results age out by kind, file reads slowest; old call arguments shrink to their size; the repository map goes once editing has started; the transcript is summarised past a token budget as the backstop.
- Skills loaded on demand, plugins that add tools, `AGENTS.md` at the root and in subdirectories.

### Configuration
- `~/.praxbot/config.toml` and a repository's `.praxbot/config.toml`, with `prax config` showing every setting and where it came from.
- Every prompt the model sees is a text file, overridable per user under `~/.praxbot/prompts/`.
- `--effort off|low|high|xhigh`, `--temperature`, `--top-p`, `--context-budget`, `--test-command`, `--lint-command`, `--protect`.

### Tools the model has
- `read_file` reads a region or one function or class whole by name; `outline`, `search`, `list_files`, `edit_file` tolerant of indentation, `write_file`, `run` with spilled output, `restore`, `todo_write`, `finish`.

### Measurement
- A ten-task suite and a 31-task SWE-bench Verified subset graded with the official harness; results and the reasoning behind every harness and prompt change are recorded in the README and under `bench/`.
