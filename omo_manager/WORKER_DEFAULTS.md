Manager worker defaults:

- Run `~/.config/getagentsmd` first and follow it.
- For non-trivial tasks, include concise process feedback before exit when instructions, routing, communication, tools, docs, or checks made the work harder than necessary.
- Report progress or blockers with `omo_report.sh`: allocate a private report file with `--alloc-message-file`, write it, then submit with `--status STATUS --message-file FILE`. Do not use `--task-file`, `--root`, `--manager-target`, or other manual route flags.
- Manage your own open-work queue only through `omo_pending.py list|add|replace|remove`; never ask for or infer its backing storage. Immediately add every still-open request you receive, keep wording close to the human's, and use `remove --item TEXT --evidence TEXT` only after verifying the item is complete or cancelled. Ask the manager for lifecycle status, ownership, routing, launch, or closure changes.
- If compaction would help you continue, ask the manager to compact or resume you; include the tmux target and what context should be preserved.
