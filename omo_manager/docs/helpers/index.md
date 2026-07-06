# shared helper reference

This directory is non-authoritative helper documentation. Manager operating instructions live in the work-log root's `MANAGER.md`; helpers use `OMO_WORK_LOGS_ROOT` to locate that file.

- `file-based-prose.md` manager-authored prose and file-input conventions
- `environment.md` Python environment and watcher setup wrapper
- `quiet-checks.md` low-token aggregate check helpers
- `agent-reports.md` durable agent-to-manager reports through `omo_report.sh`
- `report-routing-human-answers.md` concise answers about report routing, `seen`, watcher restart, unsticking, and digests
- `tmux-send.md` safe tmux paste and async delivery through `omo_tmux_send.py`

Related topic branches:
- `../mail/index.md`
- `../watchers/index.md`
- `../codex/index.md`
- `../routing/index.md`
