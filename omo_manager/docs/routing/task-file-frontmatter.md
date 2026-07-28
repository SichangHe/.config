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
- `long_running`
- `blocked`
- `done`

If `status` is `blocked` or `long_running`, `blocked_on` is required; otherwise, `blocked_on` MUST NOT exist.
ALL other fields are required.

`managerat` MUST be different from `runat`. In a worker task file, `managerat` is the manager that owns the task. In a manager task file, `runat` is the manager pane that receives pending blocks already written to that manager file. Every task's `runat` receives ordinary pending delivery; `managerat` receives only manager-marked content, matched case-insensitively with surrounding punctuation ignored.

`runat` is normally a tmux target. `runat: retired` is valid for blocked persistent roles whose pane no longer exists. As a read-only historical compatibility exception, the manager status helper also validates completed child records with `status: done` and `runat: retired` while checking whether their parent can close; it does not rewrite those records or accept that combination in active-task operations.

`long_running` is active but suppresses ordinary ready/close reminders. Use it for managers and persistent human-facing interactive agents, and describe why the role remains open in `blocked_on`. It still receives pending-item reminders and error, missing-pane, stuck-input, malformed-state, and launch-failure handling.

`pending_task_items` only contains items that are still open. Each agent manages its own queue through the path-opaque `omo_pending.py list|add|replace|remove` interface. Before removing an item, verify it is actually complete or cancelled and pass one-line evidence.

## helper behavior

Managers may record a newly delivered `(pending)` block with `omo_record_pending.py --pending-file TASK.md --line LINE --item ITEM [--item ITEM ...]` only for atomic ingress or cross-task routing. Quote the human's words as much as possible in each `--item`. Use `--ack-human` for human-origin requests so the script emails the human after it records the items and removes the consumed `(pending)` marker. Use `--email-file manager_mail/N.txt` for email-origin requests so the script uses the original email subject. Use `--task-file WORKER_TASK.md` only as a manager-side target for the initial transfer; do not put task-file paths in worker prompts. Thereafter each agent manages its own queue with `omo_pending.py`. If a human-origin consumed marker has no new pending item, clear it with `omo_task_edit.py pending-marker-clear TASK.md --line LINE --comment TEXT --ack-human --clear-kind report-only|duplicate|cancelled|superseded`; when an active owner task already tracks the request, use `--clear-kind existing-owner-item --owner-task-file OWNER.md --owner-item ITEM`, which verifies that `ITEM` is still present on `OWNER.md`. Managers use explicit-path `omo_task_edit.py` commands only for lifecycle, cross-task routing, recovery, or troubleshooting.

Managers change `status` with `omo_task_status.py TASK.md running|long_running|blocked|done`; task paths resolve under the configured work-log root. Use `--blocked-on TEXT` with `blocked` and `long_running`. The script rejects status changes while any live `(pending)` marker remains, and rejects `done` while `pending_task_items` is nonempty with a reminder to verify each item is actually complete or cancelled before removing it. It also rejects marking a manager task `done` while any active child task still has `managerat` pointing at that manager's `runat`; reassign those children first with `omo_task.py --migrate-manager-owner --old-manager-target OLD --new-manager-target NEW`. When it switches a task to `done`, it first marks the task blocked for close-in-progress, closes the task `runat` Codex pane, writes replayable close bookkeeping, writes `status: done`, and prints a reminder to email the human. If pane close fails, the task stays blocked with `done_close_failed`; retry normal `done` after fixing the pane. If close bookkeeping fails after the pane closed, the task stays blocked with `done_close_bookkeeping_failed`; rerun `omo_task_status.py --finish-closed-done --session-id SESSION TASK.md` to finish TODO movement and the close note without closing the pane again. The same finish command applies to `done_close_in_progress` or `done_close_failed` only when the task already contains the structured close note for that session.

For a stopped stale record whose `runat` is occupied by an active replacement, use `--finish-replaced-done` with an explicit replacement task, the exact evidence recorded by the stale task's verified pending-item removal, and distinctive text currently visible in the replacement pane. A stale record with no pending items may instead use an exact `(verified empty stale task: EVIDENCE)` comment. This path requires matching target, owner, role, active replacement status, and nonempty replacement pending work; it reads but never signals the reused pane. It transactionally moves the stale TODO entry and status, rolling TODO back if the task replacement fails.

`omo_task.py` creates new task files with correct placeholder frontmatter. Ordinary workers start `running`; `--is-manager` tasks start `long_running`. `managerat` is the current tmux window; `runat`, `tool`, and `is_manager` are mandatory; `pending_task_items` is empty. Each agent then manages its own queue with `omo_pending.py`, without receiving the task path.

`omo_task_edit.py summary TASK.md` gives managers an overview without reading the whole task body. Managers read task files directly only for overview or troubleshooting; routine mutations go through `omo_task_edit.py`, `omo_record_pending.py`, or `omo_task_status.py`.

`omo_task_edit.py comment-add TASK.md --message TEXT` appends `TEXT` as a parenthesized task-file comment after validating the task frontmatter.

`omo_agent_status.py` only reads from frontmatter.

`omo_pending_watch.py` scans for `(pending)` markers. Ordinary task-file messages go directly to that task's `runat`, send no manager copy, and clear the consumed marker only after verified delivery when the original block is unchanged or bounded by a later `(pending)`. `for manager` or `for a manager` at the beginning or end of active unquoted pending-block or directly linked readable content routes to `managerat`; matching ignores case, surrounding punctuation, and edge whitespace, while linked content is resolved once through the existing attachment path policy. Literal `DM` and `DM only` text has no routing meaning. The receiving agent maintains its own pending queue through `omo_pending.py`.

`omo_report.sh` infers the reporting worker task file from the current tmux pane, finds its `managerat`, and appends the `(pending)` report block to that manager's task file. Workers invoke it without `--task-file`, `--root`, `--manager-target`, or other manual route flags. If `managerat` is the main manager target, the destination is the dated `work_manager_YYYY-MM-DD.md` file.

`email_idle_watcher.py` appends an addressed human email as a route-neutral pending pointer to the addressed active task file. The pending watcher then applies the same direct-or-manager marker policy. Unaddressed manager-thread mail remains on the current main-manager file.

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
