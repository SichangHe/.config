# Codex same-pane start

`omo_codex_start.py` starts, resumes, or rotates one tracked Codex task in an existing tmux pane. It preserves the exact pane and window, requires the task to be active in `TODO.md`, verifies the task `runat` identifies that pane, requires an explicit model and reasoning effort, and waits for Codex to become `running` or `ready`. PCODX is supported only when restarting a live PCODX process whose private run and ledger state can be captured.

Rotate one tracked non-manager Codex worker into a fresh context in the same pane:

```bash
omo_codex_start.py --root ROOT \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model MODEL \
  --reasoning-effort EFFORT \
  --rotate-worker \
  --expected-task-sha256 TASK_SHA256 \
  --expected-status blocked \
  --expected-owner-target MANAGER_SESSION:WINDOW \
  --expected-pending-item 'FIRST EXACT ITEM' \
  --protected-target PROTECTED_SESSION:WINDOW \
  --audit-output PRIVATE_NEW_AUDIT_FILE
```

Repeat `--expected-pending-item` in queue order for every item and repeat `--protected-target` for the authoritative protected set. `--expected-status` accepts `blocked` or `running`. The audit parent directory must be owner-private and the file must not exist. Rotation refuses manager tasks, PCODX, `h*` sessions, the caller's pane, any requested target in the explicit protected set, missing or rebound panes, and task/target/status/owner/queue/digest drift. It captures the old session id only as evidence, respawns a fresh command without `resume`, and proves the same pane and window, a new pane process, a different new Codex session id, and unchanged task bytes after startup. The fresh prompt contains `WORKER_DEFAULTS.md` and the tracked task file; it never adds `MANAGER.md`. The private audit records the bound identities and starts with completion explicitly unknown; an atomically finalized record distinguishes success or post-respawn failure, while a finalization fault leaves durable unknown evidence and does not replace the original rotation error.

Only when the task owner has established that a legacy non-manager worker's old UUID cannot be recovered, use this exact additional assertion form:

```bash
omo_codex_start.py --root ROOT \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model MODEL \
  --reasoning-effort EFFORT \
  --rotate-worker \
  --expected-task-sha256 TASK_SHA256 \
  --expected-status blocked \
  --expected-blocker 'EXACT BLOCKER' \
  --expected-owner-target MANAGER_SESSION:WINDOW \
  --expected-pending-item 'FIRST EXACT ITEM' \
  --protected-target PROTECTED_SESSION:WINDOW \
  --audit-output PRIVATE_NEW_AUDIT_FILE \
  --assert-legacy-missing-session-id
```

This is an assertion, not a skipped UUID check. The helper must observe the UUID still missing during capture and again at the replacement boundary; if it recovers any UUID, it refuses the legacy path. It also binds and immediately revalidates the exact task bytes, status, blocker, manager owner, ordered queue, protected set, target, pane, window, pane id, process id, and command. It then starts fresh without `resume` or `MANAGER.md` and proves the unchanged lifecycle/task bindings, same pane/window, changed process, and newly captured UUID. The private audit records whether the legacy assertion was observed, the bound identity digests, and either success, failure, or durable completion-unknown when finalization itself fails. Omitting the legacy assertion preserves ordinary `--rotate-worker` behavior, including refusal when the old UUID cannot be captured.

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

Human-owned `h*` panes remain prohibited except for one byte-exact `--restart-running` authorization: root `/shagent/work_logs`, task `human_task_planner.md`, target `hwl:3`, source `manager_mail/85c5dff58359-298.txt`, and lines `1-3`. Worker rotation never accepts this exception. Both email options are required for the restart. The helper requires the fixed private file content and binds its file identity, digest, selected lines, action, target, and live pane/process identity. It revalidates that authority immediately before replacement. Every other root, task, `h*` target, source, line range, action, paraphrase, changed source, changed pane/process, missing original Codex session, or task/queue drift fails closed.

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
