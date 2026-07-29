# agent status helper

`omo_agent_status.py` uses `TODO.md` as the linked task-file index, reads each linked task file's frontmatter for authoritative status, then summarizes active tasks with tmux status helper results.

It reports `not_codex`, `running`, `error`, `ready`, or `stuck_input`; completed registry rows are stale bookkeeping and can be pruned with `--prune-completed`. By default it exits `0` after a successful summary. With `--exit-code-if-active`, it exits `3` when any task is still active, meaning linked from `TODO.md` and not marked `done` or `blocked` in its task file.

With `--problems-only`, it stays silent unless a task file with frontmatter `status: running` is `error`, `not_codex`, `ready`, or `stuck_input`, a TODO-linked active task has malformed strict metadata, a live TODO-section task file has nonempty `pending_task_items` after it is done, a blocked persistent-role task is `error`, `not_codex`, or `stuck_input`, a registry row or deduplicated `TODO.md`/task-file `runat` target that is no longer an inspected active row is visibly `error`, `not_codex`, or `stuck_input`, an agent-owned tmux pane is not tracked by any task file, the optional `--manager-target` pane is `error`, `not_codex`, or `stuck_input`, or a completed registry row is stale. `--manager-target` only adds a manager self-check; it does not filter worker tasks.

When any such problem exists, it prints only those rows, includes only nonzero problem-class counts, and exits `3`. Rows carry `owner_target=` when a task's manager is known. VL rows that lack `managerat:` are attributed to the active `vl_submanager_current_*` or `vl_supervisor_current_*` target when one is linked, so the pending watcher can dispatch each row to the responsible manager.

`malformed_task` rows preserve the strict parser error for manager repair. A scan containing one is visibility-only: it may inspect and report state, but it cannot send input, auto-unstick, interrupt, stop, or replace any pane.

Parked blocked persistent roles suppress only healthy `ready`, so an exited supervisor shell is reported as `not_codex`. Fallback rows are marked `role=registry_unmanaged`, `role=todo_unmanaged`, or `role=tmux_unmanaged` and keep the task-file status when available; missing historical tmux targets with no visible pane output are ignored. Tmux sessions starting with `h` are human-owned and ignored by agent tracking; other running, ready, errored, or stuck Codex panes without a task-file owner are `untracked_agent`.

Non-blocked `stuck_input` panes are submitted with Enter when `omo_codex_status.py` says the visible input is safe. Blocked task files are not auto-submitted.

Do not remove or mask TODO target suffixes to hide stale panes; if TODO target suffixes are cleaned, the helper uses task-file `runat` targets and scoped live `vl` error/stuck-input scanning instead of broad shell/editor `not_codex` scanning.

Persistent-role standby is durable task-file frontmatter: set `status: blocked` and put the persistent-role reason in `blocked_on`. Current persistent VL roles are `vl_supervisor_5410.md`, `vl_proof_analysis_5410.md`, `vl_spec_analysis_5410.md`, `vl_proof_runner_5410.md`, and `vl_spec_runner_5410.md`.
