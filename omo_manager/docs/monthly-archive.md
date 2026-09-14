# monthly archive

(authored by agents unless marked 🧑)

- purpose
  - keep `TODO.md` small enough for managers to scan quickly
  - archive `previous` task-index rows by month

- `TODO.md` rule
  - keep every `current`, `human pending`, and `low priority` entry in `TODO.md`
  - move every `previous` row into the prior month's `YYYYMM/old_todos.md`
    - 🧑 `You just move tasks lines from “previous” to an older dir. You don’t check the “status”.`
  - read only `TODO.md` and the affected `old_todos.md` index
  - do not read task records, statuses, frontmatter, artifacts, panes, or `runat`
  - do not move task or artifact files
  - preserve the original task-file references and short completion notes

- ownership and validation
  - include only bookkeeping paths owned by the archive operator or explicitly handed off by their owner
  - preserve unrelated dirty paths and project implementation files
  - agents may edit different files concurrently because those edits do not create a file-content merge conflict
  - the Git index is shared across the worktree, so a normal commit can include another agent's already-staged file
  - stage exact owned paths, inspect `git diff --cached --name-only`, and commit with `git commit --only -- <owned-path>...`
  - verify the committed path list and leave every other agent's staged state unchanged
  - verify that every non-`previous` section is unchanged and every `previous` row moved to the prior-month index
  - inspect the complete changed-path list and diff, then run `git diff --check` before committing

- cleanup trigger
  - when `omo_pending_watch.py` reports that `TODO.md` is too long, move every `previous` row out before doing unrelated manager work
  - the watcher validates the preview's `TODO baseline` and `retention plan`, then records the exact operation-set identity after a no-op or accepted delivery; restarts and unrelated line-count/index drift do not repeat the same operations, while changed operations rearm the reminder
  - after archiving, leave every non-`previous` section unchanged and `previous` empty
