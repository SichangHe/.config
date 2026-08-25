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

Automatic email recovery does not invoke this helper yet.
