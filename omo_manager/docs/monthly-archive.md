# monthly archive

- purpose
  - keep `TODO.md` small enough for managers to scan quickly
  - move old completed task history into monthly archive files

- `TODO.md` rule
  - keep all active `current`, `human pending`, and `low priority` tasks in `TODO.md`
  - keep only the newest 20 `previous` tasks in `TODO.md`
  - move older `previous` tasks into `YYYYMM/old_todos.md`
  - preserve the original task-file references and short completion notes

- cleanup trigger
  - when `omo_pending_watch.py` reports that `TODO.md` is too long, move done material out before doing unrelated manager work
  - after archiving, leave `TODO.md` with the active sections plus at most 20 `previous` rows
