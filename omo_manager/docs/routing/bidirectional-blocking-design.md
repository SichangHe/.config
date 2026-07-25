# bidirectional blocking design

## decision

- use versioned pending-item objects with immutable generated ids
- keep item dependency truth on each open item
- preserve resolved-item tombstones in the owning task
- guarantee at-least-once wake delivery and idempotent wake handling
- rebuild the non-durable reverse index from task files on events and periodic recovery scans
- keep bidirectional-blocking implementation separate from provenance cleanup

## schema

- schema version: `v2.0.0`
- canonical values
  - task ids: globally unique `task_` plus UUIDv7 lowercase text
  - ids: globally unique `pi_` plus UUIDv7 lowercase text
  - notice ids: globally unique `wake_` plus UUIDv7 lowercase text
  - outcomes: `completed` or `cancelled`
  - times: RFC 3339 with an explicit offset
  - YAML is emitted by the shared parser and dumper, never string editing

```yaml
version: v2.0.0
task_id: task_019f...
status: blocked
resume_status: running
blocked_on:
  - kind: pending_items
    item_ids: [pi_019f...]
pending_task_items:
  - id: pi_019f...
    text: integrate the reviewed result
    blocked_on:
      - task_id: task_019e...
        item_id: pi_019e...
        state: waiting
    notices:
      - id: wake_019f...
        kind: ready
        state: pending
        recipient_task_id: task_019f...
        target_snapshot: example:1
        attempt_count: 0
        retry_after: null
        escalated_at: null
resolved_task_items:
  - id: pi_019e...
    outcome: completed
    evidence: independent review passed
    resolved_at: 2026-07-25T14:00:00-07:00
    notices: []
```

- `resolved_task_items` is durable Git-tracked history
  - retain tombstones while any active reference exists
  - archive only after full reference validation
- top-level `blocked_on`
  - `pending_items` entries are generated from blocked own items
  - allowed entries are exactly:
    - `pending_items` with one or more `item_ids`
    - `human` with nonempty `reason`
    - `task` with canonical in-root `task` and nonempty `reason`
    - `legacy` with the exact nonempty v1 scalar in `text`
  - `human`, `task`, and `legacy` entries remain manager-owned lifecycle blockers
  - item dependencies are never duplicated at top level
- `resume_status` is required for every `status: blocked` task
  - its value is `running` or `long_running`
  - it is absent for all other states

## commands

- agent-facing, path-opaque
  - `omo_pending.py add --item TEXT` generates and prints an id
  - `omo_pending.py list` shows ids, text, and dependency state without task paths
  - `omo_pending.py remove --item-id ID --outcome completed|cancelled --evidence TEXT`
  - `omo_pending.py wake-ack --notice-id ID` atomically acknowledges a wake and prints the ready item
- manager-only, explicit path
  - `omo_task_edit.py dependency-add --task TASK --item-id ID --on-task TASK --on-item-id ID`
  - `omo_task_edit.py dependency-remove --task TASK --item-id ID --on-task TASK --on-item-id ID --evidence TEXT`
- every explicit task path resolves canonically inside the configured work-log root
- commands use paths only for lookup and persist the resolved immutable task ids
- dependency commands submit mutations to the watcher actor and fail if it is unavailable
  - only that actor writes dependency edges, one request at a time
- manager-only commands infer the caller from tmux and require one active task with `is_manager: true`
  - the edited task must name the caller's `runat` as its `managerat`
  - changing another manager's task requires ownership migration first
- errors, agent lists, and wake prompts never expose task paths

## completion and wake flow

- completion
  - require explicit outcome and evidence
  - mark every nonterminal notice `superseded`, then atomically move the whole item and its notice history to resolved tombstones
  - request immediate reconciliation after the durable write
- reconciliation
  - rebuild or update the reverse index
  - a completed dependency removes its waiting reference
  - a cancelled dependency changes the reference to `cancelled` and leaves the dependent blocked
  - the final completed dependency creates one durable pending notice
- lifecycle
  - if an item becomes ready while any external task blocker remains, retain the notice without delivery
  - otherwise, if a blocked task has only generated `pending_items` blockers and now has an actionable item, atomically remove generated blockers, restore `resume_status`, and make the notice deliverable
  - never clear human or external blockers automatically
  - every blocker edit reconciles retained notices
    - adding a lifecycle blocker supersedes any pending or acknowledged ready notice
    - removing the final lifecycle blocker creates or enables one current ready notice
  - adding an unresolved dependency is one atomic owner-task write
    - supersede the current notice, capture `running` or `long_running` in `resume_status`, set `status: blocked`, and regenerate the top-level `pending_items` blocker
    - if already blocked, retain `resume_status` and all external blockers
  - reconciliation repairs any interrupted legacy state where an unresolved item dependency lacks its generated task blocker
- delivery
  - send a stable notice id with the path-opaque `wake-ack` command
  - delivery is at least once; duplicate visible prompts are possible after a crash
  - resolve `recipient_task_id` to one active task and refresh `target_snapshot` before each send
  - `wake-ack` requires the caller's active `task_id` to equal `recipient_task_id`
  - `wake-ack` atomically rechecks that every dependency is completed and no lifecycle blocker remains
  - a stale notice is marked `superseded` and rejected
  - adding a dependency atomically supersedes any pending or acknowledged ready notice before committing the edge
  - readiness makes the item actionable; acknowledgment only records receipt
  - `wake-ack` is idempotent for a still-current notice
  - acknowledgment changes notice state to `acked`; the watcher then stops delivery

## notice transitions

- unresolved dependency: no notice
- final dependency completed with a lifecycle blocker: create `deferred`, never send it
- final lifecycle blocker removed: change `deferred` to `pending`, restore lifecycle, and send
- successful current acknowledgment: change `pending` to terminal `acked`
- any new dependency or lifecycle blocker after `pending` or `acked`: change it to terminal `superseded`
- readiness after `superseded`: create a new notice id; never revive an old id
- completion or cancellation preserves all terminal notice ids in the resolved-item tombstone

## retry and failure state

- each notice durably stores attempt count, retry deadline, and escalation time
- sender failure increments the count and retries after 1, 2, 4, 8, then 15 minutes
  - later retries remain capped at 15 minutes
  - the fifth failure creates one deduplicated manager escalation
- an unavailable target follows the same schedule and keeps the notice pending
  - target recovery makes the notice immediately eligible and clears its escalation after successful acknowledgment
  - retries continue every 15 minutes after escalation until acknowledgment or cancellation
- watcher restart resumes from durable notice state
- cancellation creates a `dependency_cancelled` notice for manager decision and never unblocks the item
- test both crash windows
  - paste succeeds before durable acknowledgment
  - durable notice exists before any paste

## graph safety

- dependency creation rejects missing tasks, missing ids, duplicate ids, self-dependencies, resolved cancellations, and cycles from its current full-graph snapshot
- the watcher validates the full graph after every relevant file change and during periodic scans
- the watcher actor serializes edge mutations and validates the full committed graph before each write
  - opposite-edge requests cannot pass the same snapshot
  - a crash leaves either the previous graph or one complete acyclic edge write
- a cycle found from unsupported manual edits fails closed
  - affected items remain blocked and receive no wake
  - owners receive one deduplicated repair notice
  - after a manager removes an edge, full validation clears the notice and reconciles readiness
- compare-and-replace writes reject stale file bytes
- no cross-file mutex or untracked lock state is required

## rollout

- phase 1: readers
  - add shared dual-read support for v1 string items and v2 objects
  - keep all writers on v1
- phase 2: migration
  - require scoped task files to be clean and committed
  - generate and commit one migration plan containing every new task and item UUIDv7, explicit blocked-task `resume_status`, and expected v1 and v2 hashes per task
  - dry-run and write both consume that immutable plan
  - convert each string item to an object with generated id and empty dependencies
  - preserve item text and order exactly
  - preserve existing task-level blockers without inferred item links
    - map each nonempty v1 scalar to one `legacy` entry with the exact scalar in `text`
    - require the owning manager to choose each blocked task's `resume_status` in the reviewed plan
  - validate all migrated bytes before one reviewed migration commit
  - each write accepts its planned v1 hash and emits the planned v2 bytes, or accepts its planned v2 hash as already complete
  - reruns finish remaining v1 entries after a partial crash; every other hash aborts
  - rollback reverts only the migration commit and its plan
- phase 3: enable
  - enable v2 writers only after every active task validates as v2
  - enable dependency commands, reconciliation, and wake delivery together
  - reject new v1 writes
- phase 4: remove transition code
  - remove v1 mutation support after archived tasks no longer enter active routing
  - retain read-only v1 parsing only for historical inspection

## worker boundaries

- bidirectional-blocking worker
  - owns shared metadata parsing, pending helpers, manager dependency commands, watcher reconciliation, migration, tests, and these docs
- provenance-cleanup worker
  - owns only separately authorized cybersecurity-refusal material cleanup
  - does not edit helper code, task schema, dependencies, or migration state

## acceptance

- ids survive add, replace, list, completion, restart, and migration unchanged
- every notice is bound to a stable task id; target moves are re-resolved before delivery
- manager dependency commands resolve only canonical in-root paths
- manager dependency commands reject callers without an active manager task and edits outside direct ownership
- worker output and prompts never expose task paths
- completing one of several dependencies does not wake the item
- completing the final dependency creates one stable notice id
- duplicate delivery leads to one idempotent `wake-ack` result
- dependency addition supersedes an old ready notice; stale acknowledgment cannot make the item actionable
- both completion-to-paste crash windows recover without lost work
- automatic unblocking restores the recorded lifecycle only when no external blocker remains
- cancellation alerts the owner and does not unblock
- retry count, deadline, and escalation deduplication survive restart
- missing, stale, malformed, self, and cyclic references fail closed with concrete errors
- serialized opposite-edge creation rejects the request that would create a cycle
- a manually introduced cycle suppresses wakes until repair and full revalidation
- migration dry-run and write are idempotent and preserve each v1 item text and order
- mixed-version readers work during rollout; v2 writers remain disabled until migration validation passes
- focused integration tests cover completion, cancellation, lifecycle-blocker add/remove, stale acknowledgment, recipient moves, caller binding, crash recovery, the exact retry schedule, target recovery, authorization, migration-plan drift, path opacity, serialized cycle rejection, and manual-cycle repair
