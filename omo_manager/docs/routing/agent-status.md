# agent status helper

`omo_agent_status.py` uses `TODO.md` as the linked task-file index, scans each linked task md bottom-up for authoritative status, then summarizes active tasks with tmux status helper results.

It reports `not_codex`, `running`, `error`, `ready`, or `stuck_input`; completed registry rows are stale bookkeeping and can be pruned with `--prune-completed`. By default it exits `0` after a successful summary. With `--exit-code-if-active`, it exits `3` when any task is still active, meaning linked from `TODO.md` and not marked `done` or `blocked` in its task file.

With `--problems-only`, it stays silent unless a task-file row marked `(running)` is `error`, `not_codex`, `ready`, or `stuck_input`, a live TODO-section task file has leftover request bullets above `(above are pending task items)` after it is no longer `(running)`, `(pending)`, or `(blocked)`, a blocked persistent-role task is `error`, `not_codex`, or `stuck_input`, a registry row or deduplicated `TODO.md`/task-file `runat` target that is no longer an inspected active row is visibly `error`, `not_codex`, or `stuck_input`, a live pane in a relevant tmux session is visibly `error` or `stuck_input`, the optional `--manager-target` pane is `error`, `not_codex`, or `stuck_input`, or a completed registry row is stale.

When any such problem exists, it prints only those rows, includes only nonzero problem-class counts, and exits `3`. Rows with `managerat:` for another manager are outside the caller's ownership. VL rows that lack `managerat:` are attributed to the active `vl_submanager_current_*` or `vl_supervisor_current_*` target when one is linked, so main-manager watchers do not consume submanager-owned worker noise.

Parked blocked persistent roles suppress only healthy `ready`, so an exited supervisor shell is reported as `not_codex`. Fallback rows are marked `role=registry_unmanaged`, `role=todo_unmanaged`, or `role=tmux_unmanaged` and keep the task-file status when available; missing historical tmux targets with no visible pane output are ignored.

Do not remove or mask TODO target suffixes to hide stale panes; if TODO target suffixes are cleaned, the helper uses task-file `runat` targets and scoped live `vl` error/stuck-input scanning instead of broad shell/editor `not_codex` scanning.

Persistent-role standby is durable task-file metadata. Accepted forms are `(blocked: persistent VL supervisor role waiting for follow-up)`, `(blocked) (persistent role waiting for follow-up)`, or adjacent split lines `(blocked)` then `(persistent role waiting for follow-up)`. Current persistent VL roles are `vl_supervisor_5410.md`, `vl_proof_analysis_5410.md`, `vl_spec_analysis_5410.md`, `vl_proof_runner_5410.md`, and `vl_spec_runner_5410.md`.
