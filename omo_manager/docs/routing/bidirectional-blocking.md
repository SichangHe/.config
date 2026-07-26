# bidirectional blocking

- purpose
  - let one pending item wait on another task's pending item
  - wake the owner safely when the final referenced item completes
  - retain human and external blockers until a manager removes them
- rollout
  - v1 task files remain readable and keep their existing string-item writers
  - `omo_task_migrate.py plan` assigns immutable task and item ids in reviewed bytes
  - planning requires every scoped task file to be tracked, clean, and committed
  - commit the generated plan before `dry-run` or `write`; commit all migrated task bytes before `enable`
  - `dry-run` accepts only each planned v1 or v2 hash
  - `write` is restart-safe and rejects every other hash
  - `enable` validates that the reviewed plan covers every active v2 task and writes the durable enablement marker
  - v2 writers, dependency edits, wake delivery, and new v2 launches remain disabled before that marker validates
- agent commands on v2 tasks
  - `omo_pending.py add --item TEXT`
    - prints the generated item id
  - `omo_pending.py list`
    - prints id, text, and `ready`, `waiting`, or `cancelled`
  - `omo_pending.py replace --item-id ID --new-item TEXT`
  - `omo_pending.py remove --item-id ID --outcome completed|cancelled --evidence TEXT`
  - `omo_pending.py wake-ack --notice-id ID`
    - rechecks recipient identity, item readiness, and lifecycle blockers
- manager commands
  - `omo_task_edit.py dependency-add --task TASK --item-id ID --on-task TASK --on-item-id ID`
  - `omo_task_edit.py dependency-remove --task TASK --item-id ID --on-task TASK --on-item-id ID --evidence TEXT`
  - explicit task paths resolve inside the configured work-log root
  - the caller must be the active manager that directly owns the edited task
  - dependency edits go through the watcher's single mutation actor
  - unavailable watcher state rejects the edit without changing an edge
- completion
  - completed references are removed during watcher reconciliation
  - the final completion creates one durable notice id
  - pending-item blockers are generated from unresolved item references
  - removing the final generated blocker restores `resume_status`
  - a human, task, or legacy blocker retains the notice as `deferred`
  - explicit lifecycle removal changes the deferred notice to `pending`
- delivery
  - the watcher owns the mutation actor, reconciles the graph on scans, and converts a due notice into an ordinary `(pending)` block
  - existing target validation, paste verification, marker clearing, and fallback delivery remain authoritative
  - watcher-generated wake blocks bypass human-instruction rendering and go directly to the current task `runat`
  - a definite delivery failure clears only the transient marker; the durable notice controls the next retry
  - prompts contain stable ids and the path-opaque acknowledgment command
  - unacknowledged notices retry after 1, 2, 4, 8, then 15 minutes
  - the notice remains durable across watcher restarts and target moves
- failure safety
  - cancellation keeps the dependency blocked and sends the owning manager a decision notice
  - a new dependency supersedes an earlier ready notice
  - missing references and cycles fail closed
  - manually introduced cycles create one stable manager repair notice per affected item and suppress ready wakes until full revalidation
  - all cooperative task writers share a per-file lock, then compare prior identity, size, and modification time before atomic replacement
  - once the durable phase-3 marker exists, active-graph drift fails closed and never re-enables v1 writers
  - every production metadata/status parse receives the configured work-log root, so task blockers cannot escape through an in-root symlink

See `bidirectional-blocking-design.md` for the reviewed schema and acceptance criteria.

## watcher compatibility audit

- `851b2b6..794f5d1` moved ordinary human mail to direct agent delivery, then added guarded asynchronous sends, immutable agent-report identity, durable report consumption, authenticated artifact isolation, and in-flight problem-alert reservation.
- blocking wakes use a separate origin-independent marker path, so they never enter human-instruction or agent-report rendering.
- wake delivery reuses current target validation and guarded cleanup; a definite failed send clears only the transient marker, while the versioned notice retains retry and escalation state.
- report-artifact deduplication and direct human email behavior remain unchanged; focused watcher regressions cover those paths alongside blocking-wake delivery.
