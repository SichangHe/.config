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

`summary TASK.md`
- print status, runat, managerat, is_manager, and pending items
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
- print a reminder that the manager must verify the item is actually done or
  cancelled, possibly by using evaluator agents

`pending-move --from MANAGER.md --to WORKER.md --item TEXT`
- validate both files before writing either file
- remove from source and add to destination
- reject done destination
- do not print the completion verification reminder, because the work remains
  open and has only moved to another queue

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
- append `(pending)`, `DM only`, and the message file content to a non-done
  worker task
- intended for managers to dispatch new worker work without hand-editing the
  worker task file
- does not send directly to tmux; `omo_pending_watch.py` owns delivery
- plain pending blocks in worker task files route to `managerat`, so this
  command MUST use `DM only` to reach the worker

## existing helper interaction

Keep `omo_record_pending.py` for the special case of consuming a pending block
and adding new pending items, because it already handles human acknowledgements
and source email subjects.

Keep `omo_task_status.py` for status changes.

`omo_pending_watch.py` prompts managers to normally use `omo_record_pending.py`
when a pending block has new work items. If there is no new item, it should point
to `omo_task_edit.py pending-marker-clear`.

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
