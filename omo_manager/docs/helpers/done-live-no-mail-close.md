purpose

- close one non-human Codex pane intentionally left live by `--complete-live-no-mail`
- preserve the already-done task and exact `TODO.md` previous-row custody
- send no Human mail and never restart or resume task work

command

- run `omo_task_status.py --close-done-live-no-mail TASK.md`
  - bind `--active-target` and `--manager-target`
  - bind `--expected-task-sha256` and `--expected-todo-sha256`
  - bind `--expected-pane-id`, `--expected-pane-pid`, and `--expected-pane-start-ticks`
  - bind `--expected-session-id` and the accepted report receipt token with `--terminal-evidence`
  - reserve a new absolute owner-private `--audit-output`

task custody

- require v1 `status: done`
- require one non-manager `tool: codex` record at the exact non-human target and manager
- require an empty ordered queue and no live `(pending)` marker
- require no active competing owner for the target
- require one exact unannotated `TASK.md TARGET` row under canonical `previous:`
- keep the TODO bytes unchanged

pane closure

- require the exact symbolic target, numeric pane, pane process, process start ticks, and Codex session
- require the accepted report token in the terminal before any input
- exit Codex only after a fresh bound `/status` response identifies the expected session
- authenticate one unchanged exited-shell capture
- launch the close child only through the tmux server's exact target, pane, and process predicate
- pass the SHA-256 of the complete canonical `terminalized` audit to the child and reauthenticate those exact bytes and the pane identity inside the child
- durably write the audit-digest/commitment-bound `.owner-close-started` marker before killing the numeric pane
- after proving both pane names and the pinned process absent, hard-link that marker to `.owner-stopped`, sync it, and remove the started name
- retain the audit-digest/commitment-bound `.owner-stopped` proof beside the audit

recovery

- `reserved` means no pane input was authorized
- `prepared` means accepted report and lifecycle evidence were checked before terminal input
- `terminalized` binds the exact exited-shell capture and close-proof commitment
- `terminalized` plus only `.owner-close-started` is retryable: reuse its secret if the exact pane is live, or promote it after exact absence
- `terminalized` plus both marker names is the retryable link-before-unlink state; both names must identify one inode
- `owner-stopped` requires the bound pane and process absent plus the durable proof
- `note-prepared` binds the only task suffix the helper may append
- `complete` requires that exact note, unchanged TODO custody, durable proof, and continued pane/process absence
- retry the identical invocation and audit path after interruption
- reject missing or mismatched marker evidence, rebound identity, changed audit bytes or capture, changed task or TODO, competing ownership, malformed audit, or an out-of-order task suffix

boundaries

- do not use this mode for running, blocked, queued, manager, v2, Cursor, PCODX, or human-owned tasks
- do not delete the completed audit or its sibling `.owner-stopped` proof; successful completion leaves no `.owner-close-started` name
- the helper never calls the completion-email path
