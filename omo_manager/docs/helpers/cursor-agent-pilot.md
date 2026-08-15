# Cursor Agent pilot

- purpose
  - let managers try Cursor Agent on a few bounded tasks before managed-worker integration
  - keep Codex as the default managed worker

- command
  - `amh_cursor_agent.py --workspace DIR --prompt-file FILE --model gpt-5.6-terra --reasoning-effort low --timeout-s 1800`
  - use `--resume SESSION_UUID` with the `session_id` returned by an earlier result
  - list locally available Cursor models with `agent models`

- behavior
  - runs Cursor Agent in noninteractive print mode with tools enabled, workspace trust accepted, and sandbox disabled
  - combines the model family and effort as Cursor's model identifier, such as `gpt-5.6-terra-low`
  - returns one `amh-cursor-agent/v1` JSON result with `ok`, and includes `result`, `session_id`, and Cursor's original result on success
  - returns the same JSON schema with `ok: false` for a missing CLI, timeout, process failure, or malformed/incomplete Cursor result

- pilot boundary
  - this is a one-shot task runner whose result returns to the calling manager
  - Cursor receives full command approval with its sandbox disabled, matching unrestricted Codex workers; `--workspace` selects context but does not contain filesystem or network access
  - it does not create a task record or tmux worker
  - do not pass `--tool agent` to `omo_task.py`; status, verified delivery, stop, and same-pane recovery are still Codex-specific
  - use the pilot for a few bounded tasks and compare its output with normal Codex work before deciding whether full managed-worker support is worthwhile
