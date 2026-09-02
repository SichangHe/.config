# External task registration

`omo_agent_status.py` normally reads only the configured watcher root's `TODO.md`. A task indexed by another root can be made visible through `omo_external_task_register.py` without copying, moving, rewriting, or duplicating its task or TODO membership.

The one authoritative registry is `${OMO_EXTERNAL_TASK_REGISTRY:-${OMO_MANAGER_STATE_DIR:-$XDG_STATE_HOME/omo-manager}/external-task-registrations}`. The helper and watcher resolve that value through the same process-environment-plus-`local.env` loader, with inherited process values taking precedence. Every prepare, apply, and rollback must use that configured path; an arbitrary second ledger is rejected. Initialize its exact private leaf once with `omo_external_task_register.py init-registry`. Initialization neither creates nor changes a task, TODO, pane, mailbox, or repository file.

## Prepare and apply

The task must already have strict frontmatter with `status: blocked`, `blocked_on: human`, `tool: codex`, `is_manager: false`, and the expected distinct `runat` and `managerat`. The external source root's `TODO.md` must contain exactly one parsed entry for the task under `current`, `human pending`, or `low priority`, and that entry must name the exact `runat` spelling. The watcher root remains unchanged. Prose mentions, previous entries, target aliases, missing or duplicate entries, and another active task with the same owner target in either root are rejected. The task and source TODO SHA-256 arguments are mandatory freshness claims.

`dry-run` validates those claims and prints the canonical plan without writing. `prepare` atomically creates one owner-private plan file with no-replace semantics. `apply` accepts only that plan's exact SHA-256 and appends one immutable registration receipt:

```text
omo_external_task_register.py prepare \
  --root /absolute/work_logs \
  --source-root /absolute/external-root \
  --task /absolute/external-root/task.md \
  --task-ref task.md \
  --runat session:3 \
  --managerat session:0 \
  --task-sha256 TASK_SHA256 \
  --todo-sha256 TODO_SHA256 \
  --output /private/packet/registration-plan.json

omo_external_task_register.py apply \
  --plan /private/packet/registration-plan.json \
  --plan-sha256 PLAN_SHA256
```

The plan and receipt bind the watcher root, external source root, the structural rule that the source task resolves outside the watcher root, the exact source `TODO.md`, external parent and task inode identities, task bytes, TODO identity and bytes, exact parsed TODO section and line, lifecycle metadata, target ownership set across both roots, authoritative registry path and inode identity, and complete registry manifest. Apply takes both roots' membership and target locks plus the task and registry locks and rechecks every binding. It rejects stale bytes, symlink substitution, another owner, duplicated task or target registration, manager or owner drift, non-blocked lifecycle drift, cross-registry copies, and registry concurrency. Publication uses a same-directory owner-private temporary, `fsync`, and an atomic hard-link no-replace operation. An exact retry returns the existing byte-identical receipt.

Apply rechecks the task, TODO, and ownership once more after receipt publication. If an uncooperative writer changes source state in the last validation-to-publication window, apply appends a receipt-bound invalidation and reports failure. The invalidated receipt stays auditable but owns nothing, and a fresh plan can bind the new source state. A later source change likewise makes the historical receipt non-current rather than permanently blocking recovery.

The watcher enumerates the unchanged external source membership only while exactly one active receipt still authenticates the watcher root, source root, task, source TODO, lifecycle, ownership set, and registry. Every registry read rechecks entry names, inode identities, and bytes, then reconstructs one unambiguous append-only history: each receipt's predecessor-manifest digest must match the already authenticated entries, and any rollback or automatic invalidation must immediately bind its exact receipt. A disconnected append, same-name replacement, duplicate task or target, missing link, ambiguous history, stale or malformed bytes, path substitution, cross-ledger copy, or deactivation resolves external tasks as absent. In-root task resolution, including through a symlinked root argument, is unchanged.

## Rollback

`rollback --receipt PATH --receipt-sha256 SHA256` appends an immutable tombstone after revalidating the original plan state and complete registry manifest. It never deletes or overwrites the receipt. An exact retry returns the same tombstone; any concurrent registry, task, TODO, owner, or path change rejects rollback and leaves all existing bytes in place.

Registration and rollback do not send pane input or mail, alter task/TODO/product bytes, run repository commands, change task lifecycle or blockers, or start/stop work. Operational approval and a final no-drift review remain separate from this mechanism.
