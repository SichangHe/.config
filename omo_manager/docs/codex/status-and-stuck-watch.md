# codex status and stuck handling

`omo_codex_status.py` reads a tmux window tail and reports `not_codex`, `running`, `error`, `ready`, or `stuck_input` plus the current response tail.

It detects the Codex TUI by `  gpt-` on the last visible line, or by a final `tab to queue message` footer paired with visible Codex running output, and extracts output between the last separator and `- Worked for ... -`.

It reports `stuck_input` when the current Codex input box contains non-placeholder text; known placeholder suggestions such as `Use /skills to list available skills` and `Run /review on my current changes` remain non-stuck. It also reports `stuck_input` for an exact terminal `Press Enter` continuation prompt only when the previous two visible lines are a completed `Worked for` line and the Codex model footer.

`status(..., detect_waiting_subagent=True)`, `report_from_lines(..., detect_waiting_subagent=True)`, and `inspect(..., detect_waiting_subagent=True)` opt into `waiting_subagent` for the exact consecutive visible lines `• Waiting for ...`, `• Working (... esc to interrupt)`, and `• Messages to be submitted after next tool call (press esc to interrupt and send immediately)`, with the Codex model footer still visible. `omo_agent_status.py --problems-only --manager-target TARGET` uses that opt-in and sends Escape only for the manager target so queued manager messages are submitted immediately.

`submit_stuck_input_if_present` rechecks the current screen, waits on tmux output while Codex is compacting, sends Enter only when the latest `stuck_input` screen is submit-safe, and otherwise returns `not_safe:REASON`.

Active stuck handling runs through the pending watcher. `omo_pending_watch.py` runs `omo_agent_status.py --problems-only` on its agent-problem interval; that status pass reports `stuck_input` rows and calls `submit_stuck_input_if_present` for non-blocked panes when the latest screen is submit-safe.

Successful status-pass submits are emitted as `unstuck: target=TARGET task=TASK action=sent_enter` and tracked by pending watcher delivery memory. Still-stuck panes are reported to the owning manager after the remembered Enter attempts are exhausted.

`omo_stuck_watch.py` remains available for manual diagnostics. It reads registered agent panes plus optional `--manager-target`, calls the status helper, stores tail hashes so repeated runs can tell whether visible output changed, and accepts `--no-auto-unstick`. One-shot mode is the default; watch mode is `omo_stuck_watch.py --watch --root ROOT --registry PATH --state PATH --manager-target TARGET`. `omo_manager_setup_watchers.sh` does not start it; setup refresh may stop same-root watch-mode instances left by old setup, and normal setup creates no `stuck-watch.log`.
