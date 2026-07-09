Manager worker defaults:

- Run `~/.config/getagentsmd` first and follow it.
- For non-trivial tasks, include concise process feedback before exit when instructions, routing, communication, tools, docs, or checks made the work harder than necessary.
- Report progress or blockers with `omo_report.sh`: allocate a private report file with `--alloc-message-file`, write it, then submit with `--status STATUS --message-file FILE`. Do not use `--task-file`, `--root`, `--manager-target`, or other manual route flags.
- Treat task files as manager-owned bookkeeping. Do not ask for task-file paths or edit task files; ask the manager to make task-file, status, or pending-item changes through manager helper scripts.
- If compaction would help you continue, ask the manager to compact or resume you; include the tmux target and what context should be preserved.
