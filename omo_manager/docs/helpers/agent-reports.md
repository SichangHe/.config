# agent report helper

`omo_report.sh` is the agent-to-manager durable path. Normal worker panes do not need a task-file path: use `REPORT_FILE=$(omo_report.sh --alloc-message-file)` to create a private draft, write that file through an editor/file-editing tool or other non-shell text channel, then run `omo_report.sh --status STATUS --message-file "$REPORT_FILE"` to submit it.

The work-log root comes from `local.env`/`OMO_WORK_LOGS_ROOT`, with `~/work_logs` as fallback. The task file is inferred from the current tmux pane/window by matching active `TODO.md` task refs to task frontmatter `runat`; if inference is ambiguous or unavailable, the report fails instead of accepting `--task-file`, `--root`, `--manager-target`, or other manual route flags. Managers should not send workers task-file paths to make reports route correctly. Do not use `cat`, heredocs, or shell text injection for report bodies.

The submit path refuses any file named exactly `REPORT` unless its parent directory is private and owner-only, because shared task/workspace `REPORT` paths can submit stale content. It writes a private detailed report artifact under `/tmp/omo-agent-messages-$UID/`, mode `0600`, then appends only a compact `(pending)` block to the target task Markdown file: `(pending)` followed by `(from agent SOURCE /tmp/omo-agent-messages-$UID/FILE.md)`.

`SOURCE` is the tmux session/window when available, such as `vl:4`, and otherwise the agent name. The `/tmp` artifact stores a concise sent line, the message SHA-256, a `message:` separator, and the report body.

The persistent task file stores no `[omo-message-source: ...]`, `(report manager ...)`, `[message-sha256: ...]`, `message-file: ...`, or report body lines for new agent-originated reports. It does not also push directly to the live manager; `omo_pending_watch.py` owns delivery from the durable Markdown block so the file remains the single source of manager notification.

If an unresolved pending block already has the same source marker and message hash in the current or historical verbose format, `omo_report.sh` exits successfully without appending a duplicate block.
