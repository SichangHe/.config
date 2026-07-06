# codex status and stuck watcher

`omo_codex_status.py` reads a tmux window tail and reports `not_codex`, `running`, `error`, `ready`, or `stuck_input` plus the current response tail.

It detects the Codex TUI by `  gpt-` on the last visible line, or by a final `tab to queue message` footer paired with visible Codex running output, and extracts output between the last separator and `- Worked for ... -`.

It reports `stuck_input` when the current Codex input box contains non-placeholder text; known placeholder suggestions such as `Use /skills to list available skills` and `Run /review on my current changes` remain non-stuck. It also reports `stuck_input` for an exact terminal `Press Enter` continuation prompt only when the previous two visible lines are a completed `Worked for` line and the Codex model footer.

`status(..., detect_waiting_subagent=True)`, `report_from_lines(..., detect_waiting_subagent=True)`, and `inspect(..., detect_waiting_subagent=True)` opt into `waiting_subagent` for the exact consecutive visible lines `• Waiting for ...`, `• Working (... esc to interrupt)`, and `• Messages to be submitted after next tool call (press esc to interrupt and send immediately)`, with the Codex model footer still visible. `omo_agent_status.py --problems-only --manager-target TARGET` uses that opt-in and sends Escape only for the manager target so queued manager messages are submitted immediately.

`submit_stuck_input_if_present` rechecks the current screen, waits on tmux output while Codex is compacting, sends Enter only when the latest `stuck_input` screen is submit-safe, and otherwise returns `not_safe:REASON`.

`omo_stuck_watch.py` reads registered agent panes plus optional `--manager-target`, calls the status helper, stores tail hashes so repeated runs can tell whether visible output changed, and uses `submit_stuck_input_if_present` for `stuck_input` panes.

It sends Enter at most once per target per pass, logs `unstick=sent_enter`, `unstick=already_sent`, `unstick=not_safe:REASON`, `unstick=failed`, or `unstick=disabled:no_auto_unstick`, and accepts `--no-auto-unstick` for diagnostics. One-shot mode is the default; watch mode is `omo_stuck_watch.py --watch --registry PATH --state PATH --manager-target TARGET`. It has no `--root` or `--once` flags.
