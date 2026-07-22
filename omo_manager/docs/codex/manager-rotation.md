# Codex manager rotation

`omo_manager_rotate.py` replaces the current main-manager Codex process with a fresh Codex session in the same tmux pane and window. It never resumes the old Codex session. It is an operator action: run it from a different pane than the manager pane.

The target must be numeric `SESSION:WINDOW` or `SESSION:WINDOW.PANE`. Window shorthand is accepted only when the exact window has one pane and that pane is index 0; it is canonicalized to `SESSION:WINDOW.0`. The helper compares tmux's resolved session, window, pane, pane ID, and window ID, rejects prefix or ambiguous resolution, and refuses to run from the target pane. It holds a private nonblocking rotation lock while it:

1. finds exactly one live `bunx @openai/codex` launch argv below the pane PID;
2. validates and normally infers its explicit model and `model_reasoning_effort`;
3. reads `~/.config/omo_manager/WORKER_DEFAULTS.md` and `ROOT/MANAGER.md`;
4. captures the existing pane output and writes the prompt plus a JSON audit record under the private manager state directory;
5. runs `tmux respawn-pane -k` against the resolved pane ID with the pane's existing working directory;
6. starts `bunx @openai/codex --dangerously-bypass-approvals-and-sandbox` with explicit model, effort, and the composed initial prompt;
7. verifies the same pane/window identity and waits for `omo_codex_status.py` to report `running` or `ready`; and
8. refreshes watchers with explicit `OMO_WORK_LOGS_ROOT`, `OMO_MANAGER_TMUX_TARGET`, and `OMO_MANAGER_STATE_DIR` values.

The generated respawn command never uses `resume` or a session UUID. The required initial prompt is stored in a mode-`0600` file and read by the fresh launch, because worker instructions themselves may discuss resuming work. State directories are mode `0700`; prompt, lock, and audit files are mode `0600`. The audit preserves the prior pane output, validated launch argv and metadata, exact pane identity, generated command, and final outcome.

## Safe invocation

From an operator pane that is not `wl:1`, use the configured manager window and work-log root:

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

An `error` startup classification fails immediately. A transient `not_codex` classification is polled until startup succeeds or times out. A timeout, pane identity change, respawn failure, or watcher failure is recorded in the audit and returned as an error. A failure after `respawn-pane` does not restore or resume the old Codex session; inspect the printed error and the newest private rotation audit.

Automatic email recovery does not invoke this helper yet.
