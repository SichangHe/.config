# supported delivery

`omo_tmux_send.py` is the shared send helper for Codex and live Cursor Agent panes. Watchers, wake loops, and managers use it; do not add a tool-specific send path.

- sendable targets
  - Codex panes in `ready`, `running`, `stuck_input`, `waiting_subagent`, or `error`
  - Cursor Agent panes whose exact process is `agent` and whose TUI shows the `Cursor … · N%` footer plus `→` follow-up composer
  - idle Cursor follow-up `Add a follow-up` is `ready`; `ctrl+c to stop` or a task count is `running`; other follow-up text is `stuck_input`
- rejection
  - Cursor chrome without a live `agent` process stays `not_codex` and is not pasted
  - that definite pre-paste rejection is retryable; the `(pending)` marker stays until a later verified send
  - do not convert, restart, or replace the worker to make send work
- verification
  - paste is checked in the follow-up composer, including collapsed `[Pasted text #N +M lines]`
  - a Cursor `follow-ups` overlay with `enter send now` is flushed with Enter
  - Cursor transcript words such as `failed` are not Codex errors
- routing
  - `omo_pending_watch.py` delivers task `(pending)` blocks through this helper to `runat` or `managerat`
  - other callers (including PB wake) must use the same helper rather than a duplicate send
