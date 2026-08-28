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

`blocked` requires `blocked_on`; `long_running` may include it. Other statuses MUST NOT include it.
ALL other fields are required.

`managerat` MUST be different from `runat`. In a worker task file, `managerat` is the manager that owns the task. In a manager task file, `runat` is the manager pane that receives pending blocks already written to that manager file. Every task's `runat` receives ordinary pending delivery. Its `managerat` receives manager-marked content, matched case-insensitively with surrounding punctuation ignored, and authenticated private reports produced by the task.

Codex worker records may include `session_id`, a UUID captured from a guarded
launch-time `/status` query. Fresh prompts are delivered only after this UUID is
captured and bound to the task. It is not valid for `pcodx` records. Existing
eligible live workers can be reviewed with `omo_codex_session_migrate.py
--root ROOT` (use `--apply` only after review). Apply accepts ready or running
Codex panes; when non-placeholder input is visible, it submits that input once
through the exact bound process before querying `/status`. Human-owned (`h*`)
targets are excluded by default. Applying to them requires `--include-human-owned`,
one repeated `--human-target` per exact `h*` target, and an owner-private
manager-mail file, selected line range, and complete-file digest that both
authorizes status capture and names every target. Pane, window, canonical
target, PID, and process command remain guarded across input submission and
status capture.

`runat` is normally a tmux target. `runat: retired` records verified historical ownership after the target disappeared. New retirement is allowed only through the digest-bound `omo_task_status.py --reconcile-missing-target` lifecycle operation with an exact owner-private direct-human authority source, selected line range, and authoritative envelope; it moves the sole TODO reference into targetless low-priority custody and never starts, stops, or reinstates a pane. The manager status helper also validates completed child records with `status: done` and `runat: retired` while checking whether their parent can close.

`long_running` is active but suppresses ordinary ready/close reminders. Use it for managers and persistent human-facing interactive agents. A `long_running` task with `blocked_on` is waiting and does not receive pending-item reminders; without it, it does. Error, missing-pane, stuck-input, malformed-state, and launch-failure handling remains active.

`pending_task_items` only contains items that are still open. Each agent manages its own queue through the path-opaque `omo_pending.py list|add|replace|remove` interface. Before removing an item, verify it is actually complete or cancelled and pass one-line evidence.

## helper behavior

Managers may record a newly delivered `(pending)` block with `omo_record_pending.py --pending-file TASK.md --line LINE --item ITEM [--item ITEM ...]` only for atomic ingress or cross-task routing. Quote the human's words as much as possible in each `--item`. Use `--ack-human` for human-origin requests so the script emails the human after it records the items and removes the consumed `(pending)` marker. Use `--email-file manager_mail/N.txt` for email-origin requests so the script uses the original email subject. Use `--task-file WORKER_TASK.md` only as a manager-side target for the initial transfer; do not put task-file paths in worker prompts. Thereafter each agent manages its own queue with `omo_pending.py`. If a human-origin consumed marker has no new pending item, clear it with `omo_task_edit.py pending-marker-clear TASK.md --line LINE --comment TEXT --ack-human --clear-kind report-only|duplicate|cancelled|superseded`; when an active owner task already tracks the request, use `--clear-kind existing-owner-item --owner-task-file OWNER.md --owner-item ITEM`, which verifies that `ITEM` is still present on `OWNER.md`. Managers use explicit-path `omo_task_edit.py` commands only for lifecycle, cross-task routing, recovery, or troubleshooting.

Managers change `status` with `omo_task_status.py TASK.md running|long_running|blocked|done`; task paths resolve under the configured work-log root. Use `--blocked-on TEXT` with `blocked`; it is optional for `long_running`. The script rejects status changes while any live `(pending)` marker remains, and rejects `done` while `pending_task_items` is nonempty with a reminder to verify each item is actually complete or cancelled before removing it. Setting `running` requires exactly one unambiguous TODO row under `current:`, `human pending:`, or `previous:` and places that unchanged row under the sole `current:` section. A status transition moves the TODO row under both file locks before committing `status: running`, and rolls the TODO move back if the task replacement fails; reissuing unchanged `running` repairs stale placement without changing task bytes. The row may omit a pane or name its authoritative `runat`. Missing, duplicate, malformed, mismatched, terminally annotated, or other-section rows fail before either file changes, and this path does not signal a pane. `--reconcile-blocked-index --source-sha256 DIGEST TASK.md` moves one unchanged v1 blocked non-manager worker with a nonempty queue from `previous` or `low priority` to `human pending`; it preserves task and pane state and rejects human/retired targets, pending delivery, failed closure, ambiguous indexes, and changed bytes. Reissuing `done` for an unchanged already-done task likewise moves its sole row from `current:` or `human pending:` to `previous:` only when no current task owns its target and its pane is absent; it preserves task bytes and never signals or stops the pane. It also rejects marking a manager task `done` while any active child task still has `managerat` pointing at that manager's `runat`; reassign those children first with `omo_task.py --migrate-manager-owner --old-manager-target OLD --new-manager-target NEW`. When it switches a task to `done`, it first marks the task blocked for close-in-progress, closes the task `runat` Codex pane, writes replayable close bookkeeping, writes `status: done`, and prints a reminder to email the human. If pane close fails, the task stays blocked with `done_close_failed`; retry normal `done` after fixing the pane. If close bookkeeping fails after the pane closed, the task stays blocked with `done_close_bookkeeping_failed`; rerun `omo_task_status.py --finish-closed-done --session-id SESSION TASK.md` to finish TODO movement and the close note without closing the pane again. The same finish command applies to `done_close_in_progress` or `done_close_failed` only when the task already contains the structured close note for that session.

Relocation and cleanup owners gate normal closure with `done --closure-repository ABSOLUTE_OWNED_WORKTREE`. This is explicit scope, not repository-ownership inference; the supplied path must be absolute and equal Git's exact worktree root, never a subdirectory. A repository with no tracked changes passes; untracked files are outside this tracked-path gate. If tracked paths are modified or deleted, also supply `--dirty-path-handoff RECEIPT.yaml`. The strict receipt contains only `version: v1.0.0`, the canonical absolute `repository`, the SHA-256 of exact `git status --porcelain=v1 -z --untracked-files=no` bytes, and `assignments`. Each assignment contains only canonical relative UTF-8 `path` (the current destination for a rename/copy), exact two-character `state`, nonempty durable `owner`, and nonempty `evidence`. Every and only current tracked dirty path must occur exactly once. Missing assignments, tracked deletions, snapshot drift, repository mismatch, malformed/non-UTF-8 paths, or a receipt against a clean repository fail before task/TODO/pane mutation. The gate is opt-in because task text does not establish repository ownership.

`omo_task_audit.py --root ROOT [--json] [--include-terminal] [--terminal-dispositions REVIEWED.yaml] [--reconciliation-queue FILE]` is a repository-wide read-only consistency audit. It deterministically reports task records with zero or duplicate TODO rows, multiple non-done records claiming one canonical run target, and separate report-only conflicts for `h*` targets. Missing-TODO findings distinguish done terminal records, blocked records naming successor task files, blocked records requiring disposition, and other active records requiring owner reconciliation. Done/no-TODO history is a single count by default; `--include-terminal` emits its record-level inventory. A reviewed terminal-disposition manifest has strict top-level `version: v1.0.0` and `records`; each unique record contains only a relative Markdown `task`, one `disposition` (`supported_closure`, `owner_disposition_required`, or `archived_dependency`), and nonempty `evidence`. It classifies a matching blocked/no-TODO record without changing it: intentional archives become `archived_dependency_no_todo` with action `none`, while the other two categories remain explicit manager actions. An entry that is absent, non-blocked, or TODO-linked yields `terminal_disposition_mismatch` and `disposition_required`; malformed manifests fail before scanning. The optional queue is an atomic canonical JSON snapshot containing only `owner_reconciliation` findings; an unchanged scan leaves it byte- and inode-identical and never sends a notice. The audit never edits a task/TODO file, sends a notice, inspects a pane, or infers closure.

Use `omo_task_status.py --root ROOT --reconcile-long-running-human-index TASK.md` only for an unchanged v1 `long_running` task whose blocker is exactly `human`. It validates the sole simple TODO row with the existing strict grammar and atomically moves it from `current:` to `human pending:` under both file locks. It does not change task bytes, status, blocker, queue, ownership, or pane state, and rejects v2 tasks, duplicates, malformed/mismatched rows, other blockers, or concurrent changes.

`--retire-blocked-target` is disabled because `retired` is not a run target, including for direct helper callers. Resolve shared-target ownership through an explicit supported lifecycle decision without fabricating a replacement target. For a legacy terminal record whose exact prior target is proven by durable Git history, `omo_task_status.py --root ROOT --restore-terminal-target --historical-target SESSION:WINDOW --historical-commit FULL40 --task-sha256 SHA256 TASK.md` restores only frontmatter `runat`. The task root must be the exact Git worktree root, and the named full commit's blob at the same relative task path must parse with that exact target and equal the current task bytes after the sole `runat: retired` substitution. The current record must remain unchanged v1 `status: done`, legacy `runat: retired`, and an empty queue. The helper rejects active/blocked records, missing or ambiguous history evidence, unrelated same-path history, commit/path/target mismatch, digest drift, and malformed metadata. It does not edit TODO, inspect or signal tmux, change status/blockers/queue/body, or guess a target.

For a blocked worker that an exact stored instruction orders stopped and retained without a linked agent, `omo_task_status.py --root ROOT --park-unlinked --expected-task-sha256 TASK_SHA256 --expected-todo-sha256 TODO_SHA256 --expected-pane-id %PANE --authority-file manager_mail/SOURCE.txt --authority-lines START-END --authority-sha256 SOURCE_SHA256 --authority-envelope ENVELOPE.md --authority-envelope-sha256 ENVELOPE_SHA256 --audit-output /PRIVATE/PARK.yaml TASK.md` performs the bounded transition. Agents and the human share one trust domain for this operation. The strict invocation supplies the machine action and binds the exact task path and digest, TODO digest, historical target, pinned pane, authority source and envelope digests, and audit path. The selected authority text is exact provenance, not input to an English-language authorization parser. The digest-bound envelope must contain exactly the selected source bytes in one `<human_instruction authoritative="true" source="SOURCE:LINES">` block. Envelope or source drift fails closed.

The authority source may be either a direct `manager_mail/FILE` or an archived `YYYYMM/manager_mail/FILE`, always relative to the task root. The helper accepts only a real calendar month, opens each component without following symlinks, rejects traversal and writable archive directories, and retains the exact complete-file digest and envelope locator checks.

The task must keep a nonempty queue, have no live pending marker, be a non-manager sole owner of an exact non-`h*` target, and have one canonical `TASK.md TARGET` row under the sole `human pending:` section plus one canonical `low priority:` destination section. Its existing blocker need not duplicate the authority locator because the invocation and audit already bind the source to the unchanged task bytes. The authority envelope may normalize CRLF mail input to LF, as the trusted intake path does; both complete files remain independently digest-bound and no other text normalization is accepted. Under membership, target, task, and TODO locks, the helper rechecks every digest, source, ownership, and file snapshot; reserves an owner-private audit receipt; and invokes the normal stop helper with both the symbolic target and numeric pane pinned through every access and close. Each pane read or mutation re-evaluates that binding in the same tmux server command queue, and closure always kills only the exact pane, never its window. The prepared audit commits to a fresh, in-memory random capability; the guarded close queues an owner-private proof of that capability only after the exact `kill-pane`. A pre-existing proof artifact is rejected before a new audit is reserved. The committed proof lets a retry advance a still-`prepared` audit if the first audit transition failed, while a guard rejection, stale artifact, or symbolic rebind without matching proof remains blocked. The helper then records the stopped-owner state before moving the sole row from `human pending:` to a bare `TASK.md` row under `low priority:`, and records completion afterward. This targetless low-priority custody marks the retained task as paused rather than ready for human action. A retry can finish the same low-priority custody move after a stop, audit/TODO write failure, or concurrent TODO change without signaling another pane. Task bytes, status, historical `runat`, blocker, queue, and body evidence remain unchanged. Stale or rebound identity, authority drift, unsafe audit state, manager/human ownership, malformed custody, and concurrent writes fail closed without overwrite. The command creates no replacement owner and must not be used merely to silence a watcher finding.

For an owner already stopped through the supported stop helper, use the same command with `--session-id UUID` instead of `--expected-pane-id`. The unchanged task must contain the exact structured close note for its historical target and that session, the target must remain absent, and its sole canonical `TASK.md TARGET` row must be under `previous:`. This mode never sends pane input or closes anything. It records a replayable prior-stop audit and moves only that row to targetless `low priority:` custody. A live or rebound target, missing or mismatched close note, non-previous source row, task/TODO drift, or audit mismatch fails before custody changes.

Status scans treat exactly one bare `low priority:` row for a blocked, non-manager, non-human worker with a nonempty queue, no pending marker, and an absent historical target as deliberate targetless custody. They do not report its historical `runat` as a missing, idle, or unmanaged live owner. A linked row, live/rebound target, empty queue, manager or human target, duplicate index row, or other section retains the normal live-target checks.

For one legacy blocked/retired non-manager worker with an empty queue whose sole `human pending:` TODO row is exactly the task path but lacks the literal `retired` marker required by the existing proven-retired closure, use `omo_task_status.py --root ROOT --normalize-retired-todo --source-sha256 SHA256 TASK.md`. It writes only that TODO row as `TASK.md retired` under task/TODO locks; it never changes task bytes, frontmatter, status, queue, history, targets, or tmux state. The exact source digest must match, and the helper rejects v2 or manager records, live pending markers, nonempty queues, any row drift/suffix/target, duplicate rows, or a row outside `human pending:`. Run the existing `--close-retired-done` command separately with its Git target proof and close-note evidence.

`--recover-exited-shell-done --pane-id PANE --session-id SESSION --terminal-evidence TOKEN TASK.md` is the bounded exception for a completed worker whose normal done close recorded that exact pane as `not_codex` after Codex exited to its original shell. It requires an empty task queue, no live pending marker, the exact close-failure text, a strict current TODO row, sole active frontmatter ownership, a fresh unchanged numeric pane, an accepted terminal report before the final interruption, the matching final resume UUID, and no shell activity after exit. It closes only that pane, records the close note, and moves the task to done/previous under the target and file locks. Managers, active Codex, ambiguous or reused shells, mismatched evidence, ownership conflicts, malformed rows, and concurrent changes are rejected without pane input.

For one stopped, blocked stale record with an empty queue and an authoritative live successor on a different target, use the narrow replacement closure:

```bash
omo_task_status.py --root ROOT --finish-replaced-done \
  --replacement-task SUCCESSOR.md \
  --stale-target STALE_SESSION:WINDOW \
  --replacement-target SUCCESSOR_SESSION:WINDOW \
  --stale-sha256 STALE_TASK_SHA256 \
  --replacement-sha256 SUCCESSOR_TASK_SHA256 \
  --replacement-status running \
  --protected-target PROTECTED_SESSION:WINDOW \
  --stopped-evidence 'EXACT EMPTY-STALE EVIDENCE' \
  --replacement-pane-evidence 'DISTINCTIVE LIVE SUCCESSOR TEXT' \
  --audit-output PRIVATE_NEW_AUDIT_FILE \
  STALE.md
```

`--replacement-status` also accepts `long_running`. Repeat `--protected-target` for the authoritative protected set. The stale task must contain the exact line `(verified empty stale task: EXACT EMPTY-STALE EVIDENCE)`. Both target values must exactly match their task frontmatter, both digests must match the current task bytes, the stale target must remain absent, and the different successor target must remain live with the supplied pane evidence. The helper rejects `h*` and alias-equivalent protected targets before pane capture. The successor must be current, have a nonempty queue, be the sole authoritative active owner of its target, and preserve the stale record's manager owner, tool, and manager role. The audit parent directory must be owner-private and the audit file must not exist. The helper locks and rechecks both task files and targets, never signals either pane, preserves successor bytes, and rolls `TODO.md` back if stale-task replacement fails. A reserved audit says completion is unknown until its final result is durably appended, so an audit-finalization failure cannot falsely claim that mutation did or did not complete.

`omo_task.py` creates new task files with correct placeholder frontmatter. Ordinary workers start `running`; `--is-manager` tasks start `long_running`. `managerat` is the current tmux window; `runat`, `tool`, and `is_manager` are mandatory; `pending_task_items` is empty. Each agent then manages its own queue with `omo_pending.py`, without receiving the task path.

`omo_task_edit.py summary TASK.md [TASK.md ...]` gives managers an overview without reading task bodies. With multiple files, it sorts by `managerat`, then the path-derived `task_file` label. Managers read task files directly only for overview or troubleshooting; routine mutations go through `omo_task_edit.py`, `omo_record_pending.py`, or `omo_task_status.py`.

`omo_task_edit.py comment-add TASK.md --message TEXT` appends `TEXT` as a parenthesized task-file comment after validating the task frontmatter.

`omo_agent_status.py` only reads from frontmatter.

`omo_pending_watch.py` scans for `(pending)` markers. Ordinary task-file messages go directly to that task's `runat`, send no manager copy, and clear the consumed marker only after verified delivery when the original block is unchanged or bounded by a later `(pending)`. `for manager` or `for a manager` at the beginning or end of active unquoted pending-block or directly linked readable content routes to `managerat`; matching ignores case, surrounding punctuation, and edge whitespace, while linked content is resolved once through the existing attachment path policy. Literal `DM` and `DM only` text has no routing meaning. The receiving agent maintains its own pending queue through `omo_pending.py`.

`omo_report.sh` infers the reporting producer task file from the current tmux pane, finds its `managerat`, and appends the `(pending)` report block to that manager's task file. This applies to worker and manager producers; a manager's report never routes back to its own producer task. Producers invoke it without `--task-file`, `--root`, `--manager-target`, or other manual route flags. If `managerat` is the main manager target, the destination is the dated `work_manager_YYYY-MM-DD.md` file.

When a reused target matches blocked historical tasks, `omo_report.sh` prefers the only `running` or `long_running` task. A sole blocked task remains reportable, and unresolved collisions fail as ambiguous.

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
