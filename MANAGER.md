# Manager Markdown pending interface

Markdown files under the work-logs root are the durable queue for manager-visible messages. Email watchers, agent reports, and manual manager notes must preserve the message in the relevant `.md` file. For ordinary agent/helper messages, the pending Markdown block plus `omo_pending_watch.py` is the single manager notification path; do not also send a separate live manager push after writing a durable pending/report block.

Pending message blocks use this forward format:

```md
(pending)
(from email manager_mail/UID.txt)
```

```md
(pending)
(from agent AGENT via omo_report.sh status=STATUS)
(report manager YYYY-MM-DD HH:MM agent=AGENT status=STATUS)
[message-sha256: SHA256_OF_MESSAGE_FILE]
message-file: /tmp/omo-agent-message-XXXXXX.md
message:
> report body read from message file
```

The parenthesized `(from ...)` line is the human-readable source marker. Helpers may add extra metadata lines after it, but must not weaken sender checks, recovery-email checks, or duplicate/idempotency protections. Direct manager pushes are reserved for concrete reliability exceptions where Markdown plus watcher cannot work; document the exact exception before using one. Agent report helpers should not append a second unresolved pending block with the same agent, status, and message hash.

Manager prompt refs include `source=email action=ack-human` for email-origin pending blocks and `source=non-email action=no-human-ack` otherwise. Only `source=email` refs require human acknowledgment. `source=non-email` refs are worker/agent/bookkeeping work: route, update Markdown, or follow up with the worker silently unless the block itself requires a human-facing answer/status. When emailing the human, describe the actual request, decision, or task in plain words; file paths, mail UIDs, and line numbers are source refs, not descriptions.

Human-facing email subjects must include the relevant task md filename so replies can be routed after manager compaction.

Current persistent VL role task files are `vl_supervisor_5410.md`, `vl_proof_analysis_5410.md`, `vl_spec_analysis_5410.md`, `vl_proof_runner_5410.md`, and `vl_spec_runner_5410.md`; idle persistent-role panes should be marked blocked standby, not running.

For tmux fallback/recovery, create a prompt file with `omo_text.py temp --kind worker-prompt`, then use `omo_tmux_send.py --target TARGET --message-file FILE [--enter]` instead of manually escaping arbitrary prompt text for `tmux send-keys`. The helper pastes via a private `0600` temp file and tmux buffer, so shell metacharacters and tmux key names inside the message remain literal. Use `--async --async-notify-target CALLER` when the manager needs the send to finish in the background and later report success/failure back to a different caller pane, such as a non-blocking worker request.

Global reporting rule: manager-facing agent reports must be low-token. Do not report how many tests passed; passing tests are assumed to pass. Verification summaries should include only aggregate pass/fail, the command names that were run, and any failures/blockers that require attention. Prefer `omo_quiet_checks.sh -- "COMMAND" [-- "COMMAND" ...]` for required tests/checks because it suppresses successful command output and prints only `checks: pass` plus command names, or `checks: fail` with the failed command and a bounded failure-output tail capped by the helper. Any repeatedly called commands or command sets should get their own tiny-output wrapper script, preferably named `*_quiet_check.*`, so agents run one stable command and see only aggregate pass/fail plus failure output.

Manager rule: when an agent has no immediate task, is about to exit, or is stopped after completion, ask for concise feedback on instructions, communication, and manager coordination. Preserve useful task-specific feedback in that task/repo Markdown; preserve generally useful feedback in manager docs, the manager tracker, or a dedicated manager feedback section/file.
