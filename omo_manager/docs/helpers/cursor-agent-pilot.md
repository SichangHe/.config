# Cursor Agent pilot

- purpose
  - let managers try Cursor Agent either as a one-shot helper or as a managed tmux worker
  - new managed workers default to Cursor Agent; pass `--tool codex` for Codex

- one-shot command
  - `amh_cursor_agent.py --workspace DIR --prompt-file FILE --model gpt-5.6-terra --reasoning-effort low --timeout-s 1800`
  - use `--resume SESSION_UUID` with the `session_id` returned by an earlier result
  - list locally available Cursor models with `agent models`

- one-shot behavior
  - runs Cursor Agent in noninteractive print mode with tools enabled, workspace trust accepted, and sandbox disabled
  - combines the model family and effort as Cursor's model identifier, such as `gpt-5.6-terra-low`
  - returns one `amh-cursor-agent/v1` JSON result with `ok`, and includes `result`, `session_id`, and Cursor's original result on success
  - returns the same JSON schema with `ok: false` for a missing CLI, timeout, process failure, or malformed/incomplete Cursor result

- managed worker command
  - use `omo_task.py --workdir DIR --model MODEL --reasoning-effort EFFORT --prompt-file FILE ...`; `--tool cursor` is the default and may be omitted
  - the launcher starts `agent --force --sandbox disabled --trust --workspace DIR --model MODEL-EFFORT`
  - the normal task file records `tool: cursor`
  - watcher status treats a live `agent` follow-up composer as `ready`, `running`, or `stuck_input`; an exact `agent` process without that chrome still counts as `running`
  - `omo_tmux_send.py` and `omo_pending_watch.py` deliver to that live pane through the Cursor follow-up composer; idle follow-up is `ready`, not `not_codex`
  - task closure uses the normal lifecycle path, but skips Codex-only `/status` probing because Cursor Agent has no compatible `/status` output

- pilot boundary
  - Cursor receives full command approval with its sandbox disabled, matching unrestricted Codex workers; `--workspace` selects context but does not contain filesystem or network access
  - `amh_cursor_agent.py` is still one-shot and does not create a task record or tmux worker
  - `omo_task.py --tool cursor` is the managed path when the task needs watcher visibility and lifecycle closure
  - Cursor managed workers do not yet have Codex-equivalent session-id capture, capacity recovery, or same-pane resume support
