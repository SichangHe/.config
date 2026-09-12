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

Repeat `--expected-pending-item` in queue order for every item and repeat `--protected-target` for the authoritative protected set. `--expected-status` accepts `blocked`, `long_running`, or `running`. The audit parent directory must be owner-private and the file must not exist. Rotation refuses manager tasks, PCODX, `h*` sessions, the caller's pane, any requested target in the explicit protected set, missing or rebound panes, and task/target/status/owner/queue/digest drift. It captures the old session id only as evidence, respawns a fresh command without `resume`, and proves the same pane and window, a new pane process, a different new Codex session id, and unchanged task bytes after startup. The fresh prompt contains captured `getagentsmd` command/output and the tracked task file; it never adds manager-role instructions for a worker. The private audit records the bound identities and starts with completion explicitly unknown; an atomically finalized record distinguishes success or post-respawn failure, while a finalization fault leaves durable unknown evidence and does not replace the original rotation error.

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

If a failed rotation audit proves the pane process was replaced but its immediate new-UUID capture failed, record later evidence with the separate reconciliation-only mode:

```bash
omo_codex_start.py --root ROOT \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --reconcile-rotation-audit \
  --rotation-audit EXACT_FAILED_AUDIT \
  --expected-rotation-audit-sha256 AUDIT_SHA256 \
  --reconciliation-receipt PRIVATE_NEW_RECEIPT \
  --expected-task-sha256 TASK_SHA256 \
  --expected-status blocked \
  --expected-blocker 'EXACT BLOCKER' \
  --expected-owner-target MANAGER_SESSION:WINDOW \
  --expected-pending-item 'FIRST EXACT ITEM' \
  --protected-target PROTECTED_SESSION:WINDOW \
  --expected-current-pane-pid CURRENT_PID \
  --expected-current-command bun
```

Repeat the pending-item and protected-target assertions for their complete ordered sets. This mode never enters a start, resume, rotation, or respawn path. Its sole pane-input exception is one bounded `/status` UUID query; every paste and Enter uses a tmux-server guard over the exact pane, window, target, PID, and command. It strictly binds one owner-private failed rotation audit, its raw bytes and file identity, the unchanged task lifecycle and ordered queue, the sole canonical `current` TODO row for that exact task, the sole active target owner, and the exact current pane/window/process. `human pending` custody is not accepted by this input-sending path. Unrelated TODO rows may differ from the historical audit; their bytes grant no authority. The receipt records both whole-file TODO digests and the bound task-row digest, while the TODO lock and repeated binding reject any change during reconciliation. The failed audit must include the helper's durable post-respawn checkpoint, the canonical `failure-kind: post-respawn-new-session-id-capture-failed` marker, and the SHA-256 of the exact `/status` response, plus inode-bound eligibility commit evidence matching the exact final audit SHA-256. New checkpoints also bind the replacement process's kernel start ticks and launch-argv digest; a checkpoint that cannot capture those fields deliberately receives no reconciliation eligibility commit. The helper writes that commit only after the audit replacement and directory sync succeed. Marker bytes left by any finalization, rollback, or removal fault remain ineligible. The helper writes the marker only after startup completed, same-pane/task validation passed, and the exact-response-only new-session query returned no UUID. Same-old and retained-unrelated-history outcomes use distinct failure kinds and never receive reconciliation eligibility. Respawn, checkpoint, startup, task/pane, unrelated, and audit-finalization failures are also ineligible. Old or ambiguous audits without this durable kind, response hash, commit, and process identity cannot be reconciled and must remain untouched. The one pre-schema incident supported by the rollout-only repair is narrowly source-bound by its exact audit digest, start ticks, argv digest, and session-metadata digest; it does not broaden ordinary historical audits or the `/status` path. The current process must be the exact observed replacement. The helper reserves a separate durable owner-private receipt before the query, requires a valid UUID distinct from the failed audit's old UUID, and revalidates every binding before success. The original failed audit, task, and TODO remain unchanged. The receipt explicitly records later evidence only; it neither rewrites the original failure nor claims why immediate capture failed. A failed reconciliation finalizes only the new receipt as failed when possible, while a receipt finalization fault leaves durable completion-unknown evidence. Reconciliation rejects `--dry-run` rather than silently performing its query or receipt write.

The exact Source-1717 stale-history incident has one separate source-bound, no-input repair. Its immutable audit digest authorizes read-only evidence only from the audited live replacement after the exact launch marker: every complete status card must show one consistent new UUID and the audited working directory, no later launch marker may exist, and the process and captured suffix must remain unchanged through receipt reservation. If current task bytes advanced while lifecycle ownership and queue stayed equal, the caller must assert both the exact current and historical audit task bindings. This exception does not accept any other audit or weaken the ordinary eligibility commit.

When that exact live replacement does not expose a UUID through `/status`, reconciliation also supports a no-input, process-held-rollout source. Add `--model`, `--reasoning-effort`, `--reconciliation-rollout`, `--session-root`, `--expected-current-pane-start-ticks`, `--expected-rollout-device`, `--expected-rollout-inode`, `--expected-rollout-holder-pid`, `--expected-rollout-holder-start-ticks`, `--expected-rollout-fd`, and `--expected-rollout-session-meta-sha256`. The two paths must be canonical absolute paths; the metadata digest covers the complete first JSONL line including LF. This variant sends no pane input. It requires the exact fresh Codex launch argv, the audit-bound pane-process start identity and argv digest, and exactly one root/user rollout held open by that replacement process tree; held reviewer-child rollouts do not create ambiguity. The asserted path, device, inode, holder PID/start ticks, descriptor, non-writable same-UID file, filename UUID, root-session metadata, working directory, and bounded rotation time must all agree. Root metadata requires ordinal zero, identical `id` and `session_id`, canonical timestamps, and no parent/fork markers. The record and payload timestamps are normally identical; the one source-bound pre-schema record has its exact observed pair committed by metadata digest. It rechecks the entire stable process tree, launch argv, and held-rollout set before accepting the distinct fresh UUID. Any `/proc` descriptor enumeration or candidate-inspection gap, directory scans, unheld rollout files, multiple root/user rollout inodes, caller-supplied UUIDs, and partial or ambiguous metadata fail closed. The receipt records every rollout and process binding and still preserves the original audit, task, TODO, ordered queue, manager, and protected-target guards.

If the manager has already recorded the failed replacement as a blocker or consolidated its queue before reconciliation, add the complete `--expected-audit-task-sha256`, `--expected-audit-status`, `--expected-audit-blocker`, `--expected-audit-owner-target`, and ordered `--expected-audit-pending-item` set. These assertions are accepted only with process-held rollout evidence or the exact Source-1717 source-bound evidence. The ordinary assertions bind the exact current task bytes, lifecycle, blocker, queue, and manager; the additional set independently reconstructs and verifies the immutable audit snapshot. When that advanced current task has completed its queue, use `--expected-current-queue-empty` instead of any current `--expected-pending-item`; this explicit empty assertion is rejected for rotation, status-query reconciliation, or rollout reconciliation without the complete original-audit bindings. The helper requires the manager target and task/TODO identity to remain the same and accepts only one active TODO row (`current` or `human pending`). It does not infer, normalize, or silently accept either queue.

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

Resumed launches pass the resolved target pane working directory through Codex's supported `--cd` option. This makes that already-bound directory explicit and prevents Codex's interactive current-versus-session directory selector from blocking the supported launch path.

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

Recover a resumed session launched outside the supported `--cd` path and paused at Codex's working-directory chooser:

```bash
omo_codex_start.py \
  --task-file TASK.md \
  --target SESSION:WINDOW \
  --model gpt-5.6-terra \
  --reasoning-effort max \
  --session-id SESSION_UUID \
  --recover-resume-cwd-prompt \
  --resume-cwd-choice current \
  --expected-session-directory SAVED_SESSION_DIRECTORY
```

Codex [documents](https://developers.openai.com/codex/cli/reference#codex-resume) this chooser when the launch directory differs from the saved session directory. This recovery accepts only the exact default menu with the asserted saved directory and pinned pane working directory, requires the pinned `bunx` process to resume the supplied UUID, and atomically rechecks pane, window, process id, and command before sending option `1` (`session`) or `2` (`current`) plus Enter. It never selects persistent options `3` or `4`, treats arbitrary `not_codex` content as authority, or accepts `h*` targets.

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

Fresh starts prepend captured `getagentsmd` command/output. Manager tasks also receive common manager instructions. A manager whose canonical `runat` equals configured `OMO_MANAGER_TMUX_TARGET` receives the main-manager role; every other manager receives the submanager role. Missing or invalid main-manager configuration stops a manager launch. The helper refuses missing, done, non-current, mismatched, non-Codex, non-shell, or same-caller tasks and panes. Tmux cannot inspect a shell's current input buffer, so `--confirm-empty-shell` is mandatory for shell starts and authorizes the helper to send Ctrl-C before pasting the launch command. Startup verification reads only output after a unique launch marker. Use `--dry-run` to validate routing and print the command without changing tmux.
