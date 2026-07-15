# task file frontmatter

Generic task metadata live in YAML frontmatter. Helper scripts manage it.

ALL task files must have frontmatter, except for the main manager task files.

## source of truth

The frontmatter is the source of truth for task metadata.

(TODO: Remove below of the section after migration.)


Legacy prose markers are migration input only:

- `runat:` becomes `runat`
- `managerat:` becomes `managerat`
- `(running)`, `(blocked...)`, and `(done...)` become `status`
- blocked reason text becomes `blocked_on`
- bullets above `(above are pending task items)` become `pending_task_items`

## draft schema

```yaml
---
version: v1.0.0
status: blocked
blocked_on: blocked reason
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items:
  - goal 1
---
```

## fields

Avoid unnecessary fields.

Reject fields that are not in the schema. All comments stay inside Markdown bodies in `()` instead.

`status` can and can only be one of:

- `running`
- `blocked`
- `done`

If `status: blocked`, `blocked_on` is required; else, `blocked_on` MUST NOT exist.
ALL other fields are required.

`managerat` MUST be different from `runat`. In a worker task file, `managerat` is the manager that owns the task. In a manager task file, `runat` is the manager pane that receives pending blocks already written to that manager file. Use worker `runat` only for the worker pane and direct-message delivery.

`pending_task_items` only contains items that are still open. Before removing an item, verify it is actually complete or cancelled; remove it immediately after that verification.

## helper behavior

Managers normally record a newly delivered `(pending)` block with `omo_record_pending.py --pending-file TASK.md --line LINE --item ITEM [--item ITEM ...]`. Quote the human's words as much as possible in each `--item`. Use `--ack-human` for human-origin requests so the script emails the human after it records the items and removes the consumed `(pending)` marker. Use `--email-file manager_mail/N.txt` for email-origin requests so the script uses the original email subject. Use `--task-file WORKER_TASK.md` only as a manager-side `omo_record_pending.py` target to record pending items on a separate running or blocked worker task; do not put task-file paths in worker prompts. If a human-origin consumed marker has no new pending item, clear it with `omo_task_edit.py pending-marker-clear TASK.md --line LINE --comment TEXT --ack-human --clear-kind report-only|duplicate|cancelled|superseded`; when an active owner task already tracks the request, use `--clear-kind existing-owner-item --owner-task-file OWNER.md --owner-item ITEM`, which verifies that `ITEM` is still present on `OWNER.md`. For routine pending-item changes, use `omo_task_edit.py pending-list|pending-add|pending-replace`; remove completed or cancelled items with `omo_task_edit.py pending-remove TASK.md --item ITEM --evidence TEXT`, which appends the evidence as a task comment and prints a verification reminder. Move existing open items between task files with `omo_task_edit.py pending-move --from MANAGER.md --to WORKER.md --item TEXT`. Keep task-file maintenance manager-owned and use helper scripts rather than routing task-file edits to workers.

Managers change `status` with `omo_task_status.py TASK.md running|blocked|done`; task paths resolve under the configured work-log root. Use `--blocked-on TEXT` only with `blocked`. The script rejects status changes while any live `(pending)` marker remains, and rejects `done` while `pending_task_items` is nonempty with a reminder to verify each item is actually complete or cancelled before removing it. When it switches a task to `done`, it first marks the task blocked for close-in-progress, closes the task `runat` Codex pane, writes replayable close bookkeeping, writes `status: done`, and prints a reminder to email the human. If pane close fails, the task stays blocked with `done_close_failed`; retry normal `done` after fixing the pane. If close bookkeeping fails after the pane closed, the task stays blocked with `done_close_bookkeeping_failed`; rerun `omo_task_status.py --finish-closed-done --session-id SESSION TASK.md` to finish TODO movement and the close note without closing the pane again. The same finish command applies to `done_close_in_progress` only when the task already contains the structured close note for that session.

`omo_task.py` creates new task files with correct placeholder frontmatter. `status` is `running`; `managerat` is the current tmux window; `runat`, `tool`, `is_manager` are mandatory arguments passed in; `pending_task_items` is empty. After a successful return, this script reminds the caller to fill in `pending_task_items` with `omo_task_edit.py pending-add`. This is manager bookkeeping and should not be handed to the worker for report routing.

`omo_task_edit.py summary TASK.md` gives managers an overview without reading the whole task body. Managers read task files directly only for overview or troubleshooting; routine mutations go through `omo_task_edit.py`, `omo_record_pending.py`, or `omo_task_status.py`.

`omo_task_edit.py comment-add TASK.md --message TEXT` appends `TEXT` as a parenthesized task-file comment after validating the task frontmatter.

`omo_agent_status.py` only reads from frontmatter.

`omo_pending_watch.py` scans for `(pending)` markers and dispatches lines from there to the end of the task file. Worker task files route normal pending blocks to `managerat`; manager task files route normal pending blocks to their own `runat`. It then remembers the dispatch in process memory before possibly re-dispatching. The target manager records `pending_task_items` with `omo_record_pending.py`, which removes the consumed pending marker after validating the file and line still match. Worker `runat` is used for direct-message worker delivery only.

`omo_report.sh` infers the reporting worker task file from the current tmux pane, finds its `managerat`, and appends the `(pending)` report block to that manager's task file. Workers invoke it without `--task-file`, `--root`, `--manager-target`, or other manual route flags. If `managerat` is the main manager target, the destination is the dated `work_manager_YYYY-MM-DD.md` file.

`email_idle_watcher.py` appends human email pending blocks to the current main manager file or to the addressed manager task file. When an addressed task is a worker task, its `managerat` is used to find the current manager file that should receive the block.

## migration plan

Migrate active task files only.

For each active task file:

1. Parse existing **bottom** `runat:` and `managerat:`.
2. Parse the latest non-pending status marker.
3. If latest status is blocked, move the reason into `blocked_on`.
4. Move bullets above `(above are pending task items)` into `pending_task_items`.
5. Add frontmatter.
6. Leave the body as is.

- [ ] finish migrating active task files
