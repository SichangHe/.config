# omo_manager helper design

`email_idle_watcher.py` writes manager emails to `manager_mail/UID.txt`, then appends a pending block to `work_manager_YYYY-MM-DD.md`. New blocks include only `(from email manager_mail/UID.txt)`; the legacy `[source: email manager_mail/UID.txt]` remains recognized for historical duplicate detection but should not be written for new email blocks. Stored `manager_mail/UID.txt` files keep the subject and body, omitting redundant self `From`, `Date`, and `UID` headers because the source marker/file name already identify the message. Normal manager emails are not pushed directly after the block is written; `omo_pending_watch.py` delivers the pending Markdown ref.

`omo_report.sh` is the agent-to-manager durable path. It appends a `(pending)` block to the target task Markdown file, marks its source as `(from agent AGENT via omo_report.sh status=STATUS)`, records `[message-sha256: ...]`, and includes the message-file contents in the Markdown block. It does not also push directly to the live manager; `omo_pending_watch.py` owns delivery from the durable Markdown block so the file remains the single source of manager notification. If an unresolved pending block already has the same source marker and message hash, `omo_report.sh` exits successfully without appending a duplicate block.

`omo_pending_watch.py` remains conservative: it scans Markdown for literal `(pending)` markers outside fenced code. Email-created, agent-created, and manual pending blocks all flow through the same watcher path.

`omo_digest_queue.py` is the durable non-urgent digest path. `submit` appends digest items to the configured queue file, records absolute `queued-at`, and records absolute `published-at` when the source provides it or a relative `Published N ago` value can be resolved at queue time. `deliver-once` sends queued items immediately when requested and renders absolute queued/published times; idle timing and recent-contact checks belong to a separate watcher. Manager human email uses `email_me.py`, which emits HTML alternatives for Markdown links while keeping a plain text fallback with bare URLs.

`omo_quiet_checks.sh` is the low-token aggregate test/check runner. Agents should run required verification as `omo_quiet_checks.sh -- "COMMAND" [-- "COMMAND" ...]` when practical. On success it prints only `checks: pass` and the command names; on failure it prints `checks: fail`, the executed command list with the failed exit status, and a bounded failure-output tail capped by the helper. Manager-facing reports must not include counts of passed tests or verbose successful test logs; include only aggregate pass/fail, command names, and failures/blockers.

For any repeatedly called command set, add a dedicated tiny-output script wrapper (for example `omo_manager_quiet_check.sh` or `*_quiet_check.py`) instead of asking agents to paste the full command list repeatedly. The wrapper may call `omo_quiet_checks.sh` internally or implement the same contract directly: successful output is suppressed, failures include only the failed command/check name and bounded failure details capped by the helper, and reports to humans include no test-success details or counts. `omo_manager_quiet_check.sh` is the aggregate validation entrypoint for this manager-helper workflow.

`omo_tmux_send.py` is the safe tmux paste primitive. Use it instead of asking agents to hand-escape arbitrary prompt text for `tmux send-keys`:

```sh
printf '%s\n' 'literal text with $HOME, `backticks`, quotes, and C-c' \
  | omo_tmux_send.py --target cfg:1.0 --enter

omo_tmux_send.py --target cfg:1.0 --message-file /tmp/instruction.md --enter
```

It reads stdin or `--message-file`, writes the payload to a private `0600` temp file, loads that file into a tmux buffer, pastes the buffer to the target, and only uses `send-keys` for optional final Enter keys. `--ready-timeout-s N` waits for a Codex idle input box before submitted paste, preventing Codex from queueing dispatch text as `Messages to be submitted after next tool call`. `--enter-count N` supports repeated submit keys. `omo_dispatch.sh --tmux-target TARGET` uses this helper for normal prompt dispatch, defaults submitted tmux dispatches to two Enter keys, and waits up to `OMO_DISPATCH_TMUX_READY_TIMEOUT_S` seconds, default `300`; override Enter count with `OMO_DISPATCH_TMUX_ENTER_COUNT`.

Dispatch rule: send prompts through the visible tmux pane and verify with manager-owned status helpers when needed. Tmux delivery is the common path; helper internals must not make manager docs depend on tool-specific transport details.

Listener/supervisor architecture note: do not merge `email_idle_watcher.py` and `omo_pending_watch.py` into one large listener as a first step. Email IDLE is an ingress adapter that writes `manager_mail/UID.txt` plus a Markdown `(pending)` block; `omo_pending_watch.py` is the single delivery path from Markdown to the manager. A robust next step is a small process supervisor/event loop that starts and health-checks watchers and owns restart/backoff/logging.

`omo_task.py` creates/links task files and can start a Codex worker in a new tmux window with `bunx @openai/codex --dangerously-bypass-approvals-and-sandbox`. Use `--reasoning-effort xhigh` to pass `--config 'model_reasoning_effort="xhigh"'`; allowed values are `low`, `medium`, `high`, and `xhigh`. Use repeatable `--codex-flag` for extra Codex argv tokens, for example `--codex-flag=--profile --codex-flag deep-review`. Use `--session-id UUID` to start `codex ... resume UUID` instead of a fresh session. It records `runat: SESSION:WINDOW codex`; pane 0 is implied.

`omo_codex_stop.py --target SESSION:WINDOW.PANE` sends Ctrl-C to a Codex pane, captures the pane tail, and prints `session_id: UUID` plus `resume_cmd: codex resume UUID` when Codex emitted a resume line. It refuses to stop the current pane unless `--allow-self` is passed.

`omo_codex_status.py` reads a tmux window tail and reports `not_codex`, `running`, `error`, or `ready` plus the current response tail. It detects the Codex TUI by `  gpt-` on the last visible line and extracts output between the last separator and `─ Worked for ... ─`.

`omo_stuck_watch.py` reads registered agent panes, calls the status helper, and stores tail hashes so repeated runs can tell whether visible output changed. It does not learn timing thresholds.

`omo_agent_status.py` summarizes active tasks from `TODO.md`/tracker plus tmux status helper results. It reports `not_codex`, `running`, `error`, or `ready`; completed registry rows are stale bookkeeping and can be pruned with `--prune-completed`.
