# Codex same-pane start

`omo_codex_start.py` starts or resumes one tracked Codex task in an existing shell-only tmux pane. It preserves the exact pane and window, requires the task to be active in `TODO.md`, verifies the task `runat` identifies that pane, requires an explicit model and reasoning effort, and waits for Codex to become `running` or `ready`.

Restart a running Codex session in the same pane with a different model or effort:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --restart-running
```

The helper captures the current session id before making any destructive change, then atomically replaces the process with `tmux respawn-pane -k`. The tmux pane and window remain the same. If session capture or task validation fails, the running process is untouched.

Resume a known Codex session from another pane:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --confirm-empty-shell \
  --session-id SESSION_UUID
```

All launches set Codex's supported `check_for_update_on_startup=false` configuration, so the startup update menu cannot block manager delivery.

Recover a resumed session that was launched before this safeguard and is paused at Codex's startup update menu:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --session-id SESSION_UUID \
  --recover-update-prompt
```

This mode recognizes only the exact Codex menu ending in `2. Skip` and `Press enter to continue`, requires the latest captured Codex launch before that menu to resume the supplied session id, and atomically rechecks the target, pane, window, and `bunx` process before sending `2` and Enter. It never respawns the pane or sends input after a mismatch. Like every lower-level start mode, it categorically rejects `h*` sessions.

Start a fresh session with task-local instructions:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --confirm-empty-shell \
  --prompt-file PROMPT
```

Fresh starts prepend `WORKER_DEFAULTS.md`; manager tasks also receive `MANAGER.md`. The helper refuses missing, done, non-current, mismatched, non-Codex, non-shell, or same-caller tasks and panes. Tmux cannot inspect a shell's current input buffer, so `--confirm-empty-shell` is mandatory for shell starts and authorizes the helper to send Ctrl-C before pasting the launch command. Startup verification reads only output after a unique launch marker. Use `--dry-run` to validate routing and print the command without changing tmux.
