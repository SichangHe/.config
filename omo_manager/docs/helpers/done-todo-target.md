# done TODO target restoration

(authored by agents unless marked 🧑)

- purpose
  - restore one historical tmux label on a targetless `previous` row
  - preserve the unchanged `done` task record
- command
  - `omo_done_todo_target.py --task TASK.md --target TARGET --task-sha256 SHA256 --todo-sha256 SHA256 --expected-previous-count COUNT`
- checks
  - task status is `done`
  - task `runat` equals the requested target
  - task and TODO bytes match the reviewed hashes
  - exactly one targetless task row is under `previous`
  - the previous-row count stays unchanged
- mutation
  - replace only `TASK.md` with `TASK.md TARGET` in `TODO.md`
  - hold the normal task-file locks and recheck bytes before replacement
  - never inspect, signal, stop, or start the historical target
