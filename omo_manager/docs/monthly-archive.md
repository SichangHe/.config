# monthly archive

- purpose
  - keep `TODO.md` small enough for managers to scan quickly
  - archive old completed-task history and eligible manager work logs together by month

- `TODO.md` rule
  - keep every `current`, `human pending`, and `low priority` entry in `TODO.md`
  - keep every blocked task, regardless of section or age
  - keep the newest 20 `previous` rows, plus any older blocked rows
  - move only older completed, unblocked `previous` rows into the matching `YYYYMM/old_todos.md`
  - use each task record's durable completion date to choose `YYYYMM`
  - preserve the original task-file references and short completion notes
  - keep task frontmatter authoritative for status, ownership, and blockers; do not recreate that metadata in the archive index

- manager work-log rule
  - in the same change, move each eligible root-level `work_manager*.md` to the `YYYYMM/` directory containing that month's completed-task records
  - a log is eligible only when all of these are true
    - it is not the active/current manager log or a log receiving current routes
    - its dated filename and dated durable contents establish one target month and agree whenever both provide evidence
    - at least one completed-task record is moving into that same `YYYYMM` archive
    - it contains no unresolved human or agent request
    - any continuing work it mentions is already recorded in a preserved authoritative task record
  - preserve the active/current manager log at its root path unconditionally, regardless of its name, date, or age
  - keep an ambiguous or multi-month log at the root until its owner confirms one destination

- ownership and validation
  - include only bookkeeping paths owned by the archive operator or explicitly handed off by their owner
  - preserve unrelated dirty paths and project implementation files
  - agents may edit different files concurrently because those edits do not create a file-content merge conflict
  - the Git index is shared across the worktree, so a normal commit can include another agent's already-staged file
  - stage exact owned paths, inspect `git diff --cached --name-only`, and commit with `git commit --only -- <owned-path>...`
  - verify the committed path list and leave every other agent's staged state unchanged
  - before moving logs, verify the active/current log from live manager configuration and routing state
  - verify that preserved TODO sections, all blocked rows, and the newest 20 `previous` rows are unchanged
  - verify each moved log has consistent evidence for one destination month, matching completed-task records, and no unresolved request
  - inspect the complete changed-path list and diff, then run `git diff --check` before committing

- cleanup trigger
  - when `omo_pending_watch.py` reports that `TODO.md` is too long, move done material out before doing unrelated manager work
  - after archiving, leave `TODO.md` with its preserved sections, all blocked rows, and the newest 20 `previous` rows
