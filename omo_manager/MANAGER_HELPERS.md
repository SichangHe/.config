# omo_manager helper design

`email_idle_watcher.py` writes manager emails to `manager_mail/UID.txt`, then appends a pending block to `work_manager.md`. New blocks include `(from email manager_mail/UID.txt)` and the legacy `[source: email manager_mail/UID.txt]`; either marker identifies the same source for duplicate detection. Normal manager emails are not pushed directly after the block is written; `omo_pending_watch.py` delivers the pending Markdown ref.

`omo_report.sh` is the agent-to-manager durable path. It appends a `(pending)` block to the target task Markdown file, marks its source as `(from agent AGENT via omo_report.sh status=STATUS)`, records `[message-sha256: ...]`, and includes the message-file contents in the Markdown block. It does not also push directly to the live manager; `omo_pending_watch.py` owns delivery from the durable Markdown block so the file remains the single source of manager notification. If an unresolved pending block already has the same source marker and message hash, `omo_report.sh` exits successfully without appending a duplicate block.

`omo_pending_watch.py` remains conservative: it scans Markdown for literal `(pending)` markers outside fenced code. Email-created, agent-created, and manual pending blocks all flow through the same watcher path.
