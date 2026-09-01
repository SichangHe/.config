# shared helper reference

This directory is non-authoritative helper documentation. Manager operating instructions live in the work-log root's `MANAGER.md`; helpers use `OMO_WORK_LOGS_ROOT` to locate that file.

- `file-based-prose.md` manager-authored prose and file-input conventions
- `environment.md` Python environment and watcher setup wrapper
- `quiet-checks.md` low-token aggregate check helpers
- `agent-reports.md` durable agent-to-manager reports through `omo_report.sh`
- `amh_problem.py claim ID --action TEXT` claims one unchanged watcher problem for exactly 10 minutes; only the watcher can resolve it
- `report-routing-human-answers.md` concise answers about report routing, `seen`, watcher restart, unsticking, and digests
- `omo_record_pending.py` records pending items from a delivered `(pending)` block, removes that marker, and optionally emails the human
- `omo_task_edit.py` manager-only task-file edits: `summary`, `pending-list`, `pending-add`, `pending-replace`, `pending-remove`, `pending-move`, `pending-marker-clear`, `comment-add`, and `delegate-message`
- `omo_task_status.py` updates task frontmatter `status`; reissuing unchanged `running`, human-blocked, or done state safely reconciles one stale TODO row
- `transcription-shared-closure.md` exact no-resend Sent adoption, incident recovery evidence, shared-`wl:32` closure, and approved post-cancellation closure
- `tmux-send.md` safe tmux paste and async delivery through `omo_tmux_send.py`
- `../routing/supported-delivery.md` shared Codex and live Cursor Agent delivery
- `cursor-agent-pilot.md` Cursor Agent one-shot helper and default managed-worker launch
- `../routing/ops-manager-cursor-replace.md` pinned same-pane Codex-to-Cursor replacement for `ops_manager.md` at `wl:3`
- `experiment-record.md` packages caller-supplied experiment files into a hashed record directory

Related topic branches:
- `../mail/index.md`
- `../watchers/index.md`
- `../codex/index.md`
- `../routing/index.md`
