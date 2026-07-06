# agent report helper

`omo_report.sh` is the agent-to-manager durable path. Reports use `omo_report.sh --task-file TASK --alloc-message-file` to create a private draft, then `omo_report.sh --task-file TASK --status STATUS --agent AGENT --message-file FILE` to submit it.

The submit path refuses any file named exactly `REPORT` unless its parent directory is private and owner-only, because shared task/workspace `REPORT` paths can submit stale content. It writes a private detailed report artifact under `/tmp/omo-agent-messages-$UID/`, mode `0600`, then appends only a compact `(pending)` block to the target task Markdown file: `(pending)` followed by `(from agent SOURCE /tmp/omo-agent-messages-$UID/FILE.md)`.

`SOURCE` is the tmux session/window when available, such as `vl:4`, and otherwise the agent name. The `/tmp` artifact stores a concise sent line, the message SHA-256, a `message:` separator, and the report body.

The persistent task file stores no `[omo-message-source: ...]`, `(report manager ...)`, `[message-sha256: ...]`, `message-file: ...`, or report body lines for new agent-originated reports. It does not also push directly to the live manager; `omo_pending_watch.py` owns delivery from the durable Markdown block so the file remains the single source of manager notification.

If an unresolved pending block already has the same source marker and message hash in the current or historical verbose format, `omo_report.sh` exits successfully without appending a duplicate block.
