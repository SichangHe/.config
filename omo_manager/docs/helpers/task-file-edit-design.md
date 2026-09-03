# task file edit helpers

Goal: managers own task lifecycle and cross-task bookkeeping, while every agent
maintains its own pending queue through a path-opaque helper.

Workers use only `omo_pending.py list|add|replace|remove`; they never receive a
task path or backing-file details. Workers report with `omo_report.sh`.

## agent pending queue

`omo_pending.py` infers the exact current tmux pane, resolves one active queue,
locks its target, rechecks ownership, and fails closed on missing or ambiguous
ownership. `list` prints item text. `add` and `replace` keep work open. `remove`
requires one-line completion or cancellation evidence. Output never includes a
task filename, `runat`, or `managerat`.

## command shape

Use one manager-side CLI:

```sh
omo_task_edit.py SUBCOMMAND ...
```

All task paths resolve under the configured work-log root. The helper validates
frontmatter, rejects unsafe transitions, and writes only when the file is
unchanged since it was read.

## subcommands

`summary TASK.md [TASK.md ...]`
- print status, runat, managerat, is_manager, and pending items for each task
- sort combined summaries by `managerat`, then path-derived `task_file` label
- validate every input before printing, so an invalid file produces no partial summary
- does not print the whole task body

`pending-list TASK.md`
- print only `pending_task_items`

`pending-add TASK.md --item TEXT [--item TEXT ...]`
- append missing pending items
- reject empty items
- reject done tasks

`pending-replace TASK.md --old-item TEXT --new-item TEXT`
- replace one existing pending item
- reject empty text
- reject missing or ambiguous old item
- reject done tasks

`pending-remove TASK.md --item TEXT [--item TEXT ...] --evidence TEXT`
- remove one or more existing pending items
- append the evidence as a task comment
- send one durable completion email containing the exact task, items, outcome,
  and evidence only when the caller is the exact responsible task owner
- when answering a Human question, accept paired subject/body files and combine
  the answer with the same immutable completion context in that one email
- require the owner-authenticated completion entry point to be a regular,
  owner-controlled executable before mutation; never fall back to invoking the
  mail helper directly when that contract is absent
- let the mail helper infer the verified producer identity; suppress human-owned
  task targets, explicit no-contact rules, and duplicate retries
- treat the exact Source-1241 safeguard phrase as meta text only when the exact
  supported manager-delegation record, intended task, Human envelope, source
  excerpt, whole task, and source file remain bound;
  every other no-contact or manager-only match still suppresses delivery
- print a reminder that the manager must verify the item is actually done or
  cancelled, possibly by using evaluator agents

Cross-state completion reconciliation:
- an owner and manager can intentionally use different
  `OMO_MANAGER_STATE_DIR` values, leaving the request and delivered markers in
  separate ledgers despite one canonical completion key
- `omo_completion_email.py --reconcile-delivered` verifies one exact owner,
  outcome, ordered pending items, evidence, current task digest, absolute
  delivered marker and marker digest, plus the unique matching source claim;
  it sends no mail and does not message the owner pane
- the operation binds the canonical message and current task bytes to the
  caller's target state and durably consumes the source receipt once; the
  canonical key and source claim bind the exact source manager target and task
  digest recorded at delivery
- a completed consumption, changed task, wrong task, owner, outcome, message or
  receipt, missing or duplicate claim, unsafe state path, or conflicting
  destination fails closed
- normal completion checks accept the target-state reconciliation only while
  its task digest and canonical message still match; ordinary
  `omo_task_status.py TASK done` remains the separate closure operation

`pending-move --from MANAGER.md --to WORKER.md --item TEXT`
- validate both files before writing either file
- remove from source and add to destination
- reject done destination
- do not print the completion verification reminder, because the work remains
  open and has only moved to another queue
- use only for initial routing, never to drain an established owner for closure

`pending-closure-transfer --from AMH.md --to MANAGER.md --source-sha256 SHA256 --destination-sha256 SHA256 --authority-file FILE --authority-sha256 SHA256`
- closure-only transfer of the source's complete established v1 queue
- require exact original source and destination byte digests plus an
  owner-private, digest-bound `manager_mail` source containing the exact
  Source-1376 shutdown instruction on one unique line
- require the AMH source to be blocked with a recorded blocker before handoff
- require active TODO custody, one active owner for each target, an `amh*`
  source, and a surviving non-AMH, non-human manager destination
- lock membership, TODO, every Markdown record under the root, the authority,
  and the recovery record before validation; unlinked active task records are
  included in owner and duplicate checks
- append the source queue after the destination queue without changing item text
  or source order; reject any transferred text already owned by another active
  queue, including duplicates in the source
- record sent/received provenance in both task bodies, including both input
  digests and the source's exact status and blocker
- durably publish one owner-private recovery record containing the exact before
  and after bytes before either replacement; a retry completes an interrupted
  transfer only when each file still equals its recorded before or after state
- write the destination then the drained source, verify both queues, TODO,
  authority, and every other locked record, and remove the recovery record only
  after both task directories are synced; caught failures rollback only exact
  bytes this operation wrote and sync those restorations before journal cleanup
- never change TODO, status, mailbox, tmux, or PCODX state; close the drained
  source separately through the normal lifecycle helper

`comment-add TASK.md --message TEXT`
- append one parenthesized comment line
- reject empty messages and messages containing newlines
- escape existing outer parentheses by wrapping the exact text as
  `(manager note: TEXT)`

`pending-marker-clear TASK.md --line LINE --comment TEXT [--ack-human] [--email-file manager_mail/N.txt] [--clear-kind KIND]`
- remove the `(pending)` marker at `LINE` without adding items
- require `--comment` so retry/idempotency has durable evidence
- append a line-bound parenthesized comment explaining why no new item was added
- for human-origin markers, require `--clear-kind`
- if `--ack-human` is present, send a short human email saying no pending item
  was added, the classification, and the reason
- before sending that email, append a human-ack-sent comment so retries do not
  send duplicate acknowledgements; remove that comment if the email command
  reports failure
- for `--clear-kind existing-owner-item`, require
  `--owner-task-file OWNER.md --owner-item ITEM` and verify that `OWNER.md` is
  active and still has `ITEM`
- use `--email-file manager_mail/N.txt` when present so the acknowledgement
  stays in the original email thread

`delegate-message WORKER.md --message-file FILE`
- append `(pending)`, a manager source marker, and the message file content to
  a non-done worker task
- intended for managers to dispatch new worker work without hand-editing the
  worker task file
- does not send directly to tmux; `omo_pending_watch.py` owns delivery
- ordinary pending blocks route directly to the task's `runat`

## existing helper interaction

Keep `omo_record_pending.py` for the special case of consuming a pending block
and adding new pending items, because it already handles human acknowledgements
and source email subjects.

Keep `omo_task_status.py` for status changes.

New transitions to `done` use a two-phase exact-owner policy. A manager queues
the owner-authenticated executable callback and returns without closing or
changing status. After the owner delivers the exact notice, the manager retries
and the durable delivery marker permits closure. A manager cannot send a
fallback on the task owner's behalf. Reissuing an already-done task does not
retroactively send mail.
Claims are persisted before mail invocation, so a retry cannot duplicate an
uncertain delivery.

`omo_pending_watch.py` sends ordinary pending blocks directly to the task agent,
which maintains its own queue through `omo_pending.py`. A message selected by a
case-insensitive, whitespace- and punctuation-insensitive manager edge marker prompts the manager to use `omo_record_pending.py` when it creates work. If it creates no new item, the manager uses
`omo_task_edit.py pending-marker-clear`.

Keep existing `list`, `add`, `replace`/`update`, `remove`, and `comment` names
as compatibility aliases if already shipped, but docs should use the canonical
names above.

## instruction updates

Manager instructions should say:
- managers use helper scripts for task-file mutations
- managers read task files only for overview or troubleshooting
- managers do not tell workers task-file paths
- workers report with `omo_report.sh` without route flags

Helper docs should list the manager-only edit commands and keep worker-facing
docs free of task-file route flags.
