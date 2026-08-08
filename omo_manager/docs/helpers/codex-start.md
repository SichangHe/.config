# Codex same-pane start

`omo_codex_start.py` starts or resumes one tracked Codex task in an existing shell-only tmux pane. It preserves the exact pane and window, requires the task to be active in `TODO.md`, verifies the task `runat` identifies that pane, requires an explicit model and reasoning effort, and waits for Codex to become `running` or `ready`. PCODX is supported only when restarting a live PCODX process whose private run and ledger state can be captured.

Restart a running Codex session in the same pane with a different model or effort:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --restart-running
```

The helper captures the current Codex session id before making any destructive change, then atomically replaces the process with `tmux respawn-pane -k`. The server-side guard binds the target, pane, window, process id, and command. The helper rechecks exact task bytes and `pending_task_items` before replacement, then proves the same pane, a new process, the resumed Codex session, and unchanged task/queue after startup. PCODX restarts also preserve and verify `PCODX_POC_ROOT`, run directory, ledger path, and PCODX session id. If any binding or session capture fails before replacement, the running process is untouched.

Human-owned `h*` panes remain prohibited except for one byte-exact authorization: root `/shagent/work_logs`, task `human_task_planner.md`, target `hwl:3`, action `--restart-running`, source `manager_mail/85c5dff58359-298.txt`, and lines `1-3`. Both email options are required. The helper requires the fixed private file content and binds its file identity, digest, selected lines, action, target, and live pane/process identity. It revalidates that authority immediately before replacement. Every other root, task, `h*` target, source, line range, action, paraphrase, changed source, changed pane/process, missing original Codex session, or task/queue drift fails closed.

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

All launches set Codex's supported `check_for_update_on_startup=false` configuration, directly for Codex and inside the PCODX wrapper, so the startup update menu cannot block manager delivery.

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

This mode recognizes only the exact Codex menu ending in `2. Skip` and `Press enter to continue`, requires the latest captured Codex launch before that menu to resume the supplied session id, and atomically rechecks the target, pane, window, captured pane process id, and `bunx` command before sending `2` and Enter. It never respawns the pane or sends input after a mismatch. Update-prompt recovery and its lower-level input helpers categorically reject `h*` sessions.

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
