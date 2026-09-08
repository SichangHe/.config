# Codex manager rotation

`omo_manager_rotate.py` replaces the current main-manager Codex process with a fresh Codex session in the same tmux pane and window. It never resumes the old Codex session. The same command works from an operator pane or from the manager process in the exact target pane.

The target must be numeric `SESSION:WINDOW` or `SESSION:WINDOW.PANE`. Window shorthand is accepted only when the exact window has one pane and that pane is index 0; it is canonicalized to `SESSION:WINDOW.0`. The helper compares tmux's resolved session, window, pane, pane ID, and window ID and rejects prefix or ambiguous resolution. It holds a private nonblocking rotation lock while it:

1. finds exactly one live supported Codex launch argv below the pane PID, accepting both `@openai/codex@latest` and an existing legacy `@openai/codex` process;
2. validates and normally infers its explicit model and `model_reasoning_effort`;
3. reads `~/.config/omo_manager/WORKER_DEFAULTS.md` and `ROOT/MANAGER.md`;
4. captures the existing pane output and writes the prompt plus a JSON audit record under the private manager state directory;
5. runs `tmux respawn-pane -k` against the resolved pane ID with the pane's existing working directory;
6. starts `bunx @openai/codex@latest --dangerously-bypass-approvals-and-sandbox` with explicit model, effort, and the composed initial prompt;
7. verifies the same pane/window identity and waits for `omo_codex_status.py` to report `running` or `ready`; and
8. refreshes watchers with explicit `OMO_WORK_LOGS_ROOT`, `OMO_MANAGER_TMUX_TARGET`, and `OMO_MANAGER_STATE_DIR` values.

The generated respawn command never uses `resume` or a session UUID. The required initial prompt is stored in a mode-`0600` file and read by the fresh launch, because worker instructions themselves may discuss resuming work. State directories are mode `0700`; prompt, lock, and audit files are mode `0600`. The audit preserves the prior pane output, validated launch argv and metadata, exact pane identity, generated command, and final outcome.

When invoked from the target pane, the first helper performs the complete non-mutating preflight and creates an exclusive mode-`0600` token reservation while holding the main rotation lock. Any other rotation rejects an active reservation. It then starts a short-lived detached coordinator window in the same tmux session and passes a canonical target plus fully explicit root, state directory, model, reasoning effort, timeout values, and the private token. Arguments and paths are shell-quoted.

READY and GO use per-token tmux lock channels so an early signal cannot be lost. Both sides wait for a bounded time. The coordinator must have the reservation's different pane ID, disables `remain-on-exit`, records and signals READY, and waits for GO. The parent requires the matching READY state and signals GO while it still owns the main lock, then releases that lock. Only the matching coordinator token may wait for and acquire it. After acquiring the main lock, the coordinator clears the reservation and performs the respawn, startup verification, watcher refresh, and final audit write. A stale reservation is removed only when its recorded coordinator pane no longer exists, or when the parent died before recording a pane and no process holds the main rotation lock.

The original manager and its helper may be killed by `respawn-pane`; neither is responsible for work after GO. A successful self-invocation response means only that the coordinator handoff reached GO, not that rotation or startup eventually succeeded. Coordinator output is appended to the reported mode-`0600` log under `STATE_DIR/coordinators/`. Once rotation preparation begins, its outcome is also stored in the newest private audit under `STATE_DIR/rotations/`. Inspect both after a self-initiated rotation. The temporary window closes when the coordinator exits. Before GO, startup, READY, or timeout failures cause the parent to remove its reservation and coordinator pane; after GO, failures belong to the coordinator and remain visible in its log and, where created, its audit.

## Safe invocation

From either an operator pane or the manager in `wl:1`, use the configured manager window and work-log root:

```bash
~/.config/omo_manager/omo_manager_rotate.py \
  --target wl:1 \
  --root /home/sichangheagent/work_logs
```

`--target` defaults to `OMO_MANAGER_TMUX_TARGET`; `--root` defaults to `OMO_WORK_LOGS_ROOT`, then `~/work_logs`. The private state directory defaults to `OMO_MANAGER_STATE_DIR`, then `$XDG_STATE_HOME/omo-manager` or `~/.local/state/omo-manager`.

Normally do not pass launch metadata. If no live launch argv exists, or the one live argv lacks either field, both overrides are mandatory:

```bash
~/.config/omo_manager/omo_manager_rotate.py \
  --target wl:1 \
  --root /home/sichangheagent/work_logs \
  --model gpt-5.6-terra \
  --reasoning-effort xhigh
```

Overrides are rejected when both values can be inferred. When only one value can be inferred, the supplied pair must agree with that inferred value. Missing, duplicate, ambiguous, unsupported, or conflicting metadata stops the operation before tmux mutation.

An `error` startup classification fails immediately. A transient `not_codex` classification is polled until startup succeeds or times out. For external rotation, a timeout, pane identity change, respawn failure, or watcher failure is returned as an error. For self-rotation, the initial command returns after the verified coordinator handoff; every later failure is written to the reported coordinator log, and an audit is also written once rotation preparation reaches audit creation. A failure after `respawn-pane` does not restore or resume the old Codex session.

## Failed legacy rotation containment

Do not reconcile a legacy manager-rotation audit that omitted task, ordered-queue, ownership, or session-UUID bindings. If its fresh successor is still live, use `~/.config/omo_manager/omo_manager_rotation_contain.py` from a different pane. The helper treats all post-failure work as unauthenticated and never adopts it. It binds the exact failed audit and watcher-failure log by SHA-256; matches the embedded old launch prompt to a hash-bound old Codex transcript; matches the fresh prompt, live launch argv, and explicit new UUID to the current transcript; and requires the same target, pane, window, changed process, blocked manager task, exact ordered queue, reporting parent, sole active target owner, and sole canonical `current:` TODO row.

The helper also proves that the canonical-root pending-watcher flock is currently held by exactly one live supported `omo_pending_watch.py` process whose `--root` resolves to the requested work-log root. An unlocked stale file, another script, an unrelated root, multiple lock owners, or process/lock drift is rejected. This proof is what permits the failed rotation's duplicate-root watcher startup to remain skipped; the legacy failure log alone is not proof of a healthy watcher.

First run the complete command with `--dry-run`. Supply either every ordered queue item with repeated `--expected-pending-item` options or, explicitly, `--expect-empty-queue`. Repeat `--protected-target` for the complete protected set. The audit, watcher log, old transcript, prompt, task, old/new IDs, old/new PIDs, pane/window IDs, blocker, reporting parent, and receipt path are all mandatory assertions:

```bash
~/.config/omo_manager/omo_manager_rotation_contain.py \
  --contain-failed-rotation \
  --root ROOT \
  --task-file MANAGER_TASK.md \
  --target SESSION:WINDOW \
  --failed-audit PRIVATE_FAILED_AUDIT \
  --failed-audit-sha256 AUDIT_SHA256 \
  --watcher-failure-log PRIVATE_FAILURE_LOG \
  --watcher-failure-log-sha256 LOG_SHA256 \
  --fresh-prompt-sha256 PROMPT_SHA256 \
  --old-session-transcript REAL_OLD_JSONL_PATH \
  --old-session-transcript-sha256 OLD_JSONL_SHA256 \
  --expected-old-session-id OLD_UUID \
  --current-session-transcript REAL_CURRENT_JSONL_PATH \
  --expected-current-session-id CURRENT_UUID \
  --expected-task-sha256 TASK_SHA256 \
  --expected-blocker BLOCKER_TASK.md \
  --expected-manager-target PARENT_TARGET \
  --expect-empty-queue \
  --expected-old-pane-id %PANE \
  --expected-old-window-id @WINDOW \
  --expected-old-pane-pid OLD_PANE_PID \
  --expected-old-launch-pid OLD_CODEX_PID \
  --expected-current-pane-pid CURRENT_PANE_PID \
  --expected-current-command bunx \
  --protected-target PROTECTED_TARGET \
  --receipt-output PRIVATE_NEW_RECEIPT \
  --dry-run
```

Remove only `--dry-run` after independent review and immediately before containment. The mutation is one tmux-server-guarded `respawn-pane -k` against the pinned non-`h*` pane and process. It preserves the pane, window, and working directory but replaces Codex with `sleep infinity`, verifies that the entire old process session/group disappeared, rechecks task/TODO/ownership/watcher bindings, and finalizes the owner-private receipt. The completed receipt says explicitly that the legacy audit was not reconciled and post-failure work was not accepted. A failed or incomplete receipt does not authorize resumption. Start a fresh, task-bound manager only through a separately reviewed lifecycle operation after the containment receipt is complete.

## Launch after successful containment

Use `~/.config/omo_manager/omo_manager_containment_launch.py` only for the inert `sleep infinity` sentinel created by a complete `omo-manager-rotation-containment/v1` receipt. The bridge consumes that exact receipt by SHA-256 and independently revalidates its original failed audit, launch executable, prompt, old and failed-successor transcripts, task bytes, ordered queue, sole task owner, canonical `current:` TODO row, reporting parent, canonical root watcher, same pane/window/working directory, failed-process absence, exact sentinel PID/start/argv, and complete protected-target set. An identical task rewrite may change inode/mtime, and unrelated TODO rows may change, but bound task bytes or the bound TODO row may not.

Supply every receipt-derived identity explicitly and first run the complete command with `--dry-run`:

```bash
~/.config/omo_manager/omo_manager_containment_launch.py \
  --launch-contained-successor \
  --root ROOT \
  --task-file MANAGER_TASK.md \
  --target SESSION:WINDOW \
  --containment-receipt PRIVATE_COMPLETE_CONTAINMENT_RECEIPT \
  --containment-receipt-sha256 CONTAINMENT_SHA256 \
  --session-root REAL_CODEX_SESSION_ROOT \
  --expected-contained-pane-id %PANE \
  --expected-contained-window-id @WINDOW \
  --expected-contained-pid SENTINEL_PID \
  --expected-contained-start-ticks SENTINEL_START_TICKS \
  --expected-contained-argv-sha256 SENTINEL_ARGV_SHA256 \
  --expected-failed-successor-command bunx \
  --expected-task-sha256 TASK_SHA256 \
  --expected-blocker BLOCKER_TASK.md \
  --expected-manager-target PARENT_TARGET \
  --expect-empty-queue \
  --expected-watcher-pid WATCHER_PID \
  --expected-watcher-start-ticks WATCHER_START_TICKS \
  --protected-target PROTECTED_TARGET \
  --ownership-receipt PRIVATE_NEW_OWNERSHIP_RECEIPT \
  --dry-run
```

Use repeated `--expected-pending-item` instead of `--expect-empty-queue` for a nonempty queue, preserving order. Repeat `--protected-target` for the exact receipt-bound set. After independent review, re-run the digest-bound preflight and remove only `--dry-run` for one execution. The bridge takes the manager-rotation lock, membership lock, target/protected-target locks, and task/TODO locks; writes a prepared receipt; and performs one tmux-server-guarded same-pane replacement of the exact sentinel. It reuses the receipt-bound prompt, model, reasoning effort, and verified resolved target of the absolute `bunx` path while retaining the original `bunx` process argv, disables the startup update prompt, and verifies the existing canonical watcher instead of starting another watcher.

The ownership receipt path is deterministic: replace the containment receipt's `.receipt` suffix with `-successor-ownership.receipt` in the same private rotations directory. The helper rejects any other output path and any pre-existing output, making the bridge a one-shot operation for that containment receipt.

Success requires one supported Codex launcher process group, the exact target/root/state environment, `ready` or `running` status, unchanged task/queue/TODO/watcher/protected bindings, and one new rollout transcript held open by that process tree. The transcript must be under the asserted session root, postdate the bridge attempt, match the exact launch prompt and working directory, and contain a UUID different from both historical sessions. The final owner-private receipt records that UUID, its process-held file descriptor, the fresh pane/process tree, environment hash, final task/watcher/protected evidence, and sole-owner counts. If launch or any later proof is uncertain, the helper attempts an exact guarded rollback to a new inert sentinel and writes a failed receipt; it never adopts an unverified session. A failed, prepared, or missing ownership receipt does not authorize task unblocking or downstream work.

Automatic email recovery does not invoke this helper yet.

## Close successful containment without a successor

When direct Human authority calls for closing a contained manager instead of launching a replacement, use `omo_manager_containment_close.py`. It accepts the same complete containment receipt and historical assertions, plus exact current empty-queue task/TODO bytes and a digest-bound authoritative-Human email envelope whose direct imperative literally names the exact tmux target. Its dry run is read-only. Its live path can close only the exact non-`h*`, childless `sleep infinity` sentinel in a one-pane window, records intent before the guarded close, proves both the pane and process session/group absent, and produces a deterministic private complete receipt stating that no successor was launched. It does not edit task/TODO lifecycle state; do that separately after the close receipt is complete. See [contained sentinel close](../helpers/containment-close.md).
