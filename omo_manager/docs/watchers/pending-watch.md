# pending watcher

- purpose
  - deliver new Markdown `(pending)` markers with enough context to act immediately
  - keep delivery memory process-local and time-bounded
  - run agent-problem and digest maintenance without delaying marker scans

- file-change path
  - Linux uses recursive inotify watches on the work-log root
  - `.git`, `.venv`, and `__pycache__` dirs are ignored
  - Markdown file events enqueue only changed files for marker parsing
  - `manager_mail/*.txt` events force a full Markdown scan so email attachment retries wake promptly
  - directory create, move, watch removal, unmount, or queue overflow forces a full Markdown scan
  - a filesystem notification resets the mtime-poll backstop even when no Markdown file changed
  - platforms without inotify use the older mtime scan fallback

- safety scans
  - startup scans all Markdown files
  - periodic full scans remain controlled by `--full-scan-interval-s`
  - full scans refresh the fallback mtime snapshot
  - while inotify is active, `--poll-backstop-interval-s` runs an mtime scan after 30 seconds without a filesystem notification, full scan, or previous backstop poll

- subprocess isolation
  - pending dispatch calls the `omo_tmux_send.py` library sender in-process
  - agent-problem checks run as background child processes and are polled
  - digest delivery runs as a background child process and is polled
  - timeout handling kills overdue maintenance children and logs stderr

- assumptions
  - production manager hosts are Linux and support inotify
  - a 30-second mtime backstop is cheap enough for normal work-log roots
  - a periodic full scan is still required as broader recovery
  - maintenance command output remains small enough for captured pipes

- pending ref semantics
  - scans Markdown for literal `(pending)` markers outside fenced code
  - inspects each pending block for explicit source markers
  - normal manager deliveries start by telling the manager to run `omo_record_pending.py`
  - human-origin manager deliveries include `--ack-human` so recording the pending items also emails the human
  - email-origin manager deliveries also include `--email-file manager_mail/N.txt` so `omo_record_pending.py` can reuse the original email subject
  - human-origin manager deliveries tell the manager to quote the human's words as much as possible when choosing `--item` values
  - agent-origin manager deliveries tell the manager to quote the request's words as much as possible when choosing `--item` values
  - agent-origin manager deliveries say to omit `--ack-human`
  - if no pending task item should be added, manager deliveries point to `omo_task_edit.py pending-marker-clear`; human-origin clears require `--clear-kind`, and `existing-owner-item` verifies the cited active owner task item; existing pending-item cleanup uses `omo_task_edit.py pending-replace` or `omo_task_edit.py pending-remove --evidence TEXT`
  - includes the pending line and content from that line to end of file
  - labels pending content as `<snippet file="PATH:START-END">`
  - truncates long content to 2000 chars by keeping start and end with `…Nchars…` in the middle
  - attaches referenced file content, including `manager_mail/*.txt` and absolute `/tmp/omo-agent-messages-*/*` report files
  - labels attached content as `<snippet file="PATH:START-END">`
  - always uses line ranges; no delivery label says `EOF`
  - file references inside quote lines are ignored
  - email source markers are `origin=human source=email`
  - explicit agent source markers anywhere in the same pending block are `origin=agent source=agent`
  - new agent source blocks use compact `(from agent ...)` markers
  - verbose `[omo-message-source: ...]` markers remain recognized for old blocks
  - unmarked pending blocks are `origin=human source=manual` because prompts appended to `work_manager*.md` are human-origin unless explicitly marked otherwise
  - human-origin refs require manager email acknowledgement
  - worker task-file frontmatter routes normal pending blocks to `managerat`
  - manager task-file frontmatter routes normal pending blocks to that manager's `runat`
  - if a resolved manager delivery target is unavailable and differs from `OMO_MANAGER_TMUX_TARGET`, the same manager-facing message is escalated to `OMO_MANAGER_TMUX_TARGET` with the failed target and error inline
  - a pending block or any readable linked file starting or ending with standalone case-insensitive `for manager`, after quote lines are ignored and edge punctuation/whitespace is trimmed, routes to the task frontmatter `managerat`
  - worker `runat` is used for DM worker delivery
  - legacy prose metadata remains recognized only for main manager task files and old explicit source markers
  - a pending block or any readable linked file starting or ending with standalone `DM`, after quote lines are ignored and edge punctuation/whitespace is trimmed, is delivered to the task frontmatter `runat` worker target when that target is safely distinct from the manager target
  - `DM only` follows the same marker rules as `DM` but does not send the manager FYI copy after successful worker delivery
  - successful DM worker delivery also sends the manager an FYI copy that starts with the same `omo_record_pending.py` instruction and ends ``this message is already dispatched to the agent, this is FYI:``
  - agent-origin DM manager FYI copies say to omit `--ack-human`
  - worker DM delivery contains only the cleaned message text
  - worker DM delivery does not include manager instructions, task-file snippets, source snippets, or XML-style wrappers
  - worker DM delivery strips standalone file-pointer lines after including readable file content, and extracts only the `message:` body from `omo_report.sh` agent report files
  - manager DM FYI keeps the task-file snippet inline so the manager can record and clear the pending marker
  - failed or unroutable DM worker delivery sends the manager an action-required fallback
  - same-process DM manager-FYI retry does not resend the worker copy after worker delivery succeeds

- manager delivery example
  - ``Normally record pending items and remove the consumed `(pending)` marker by running:``
  - ``omo_record_pending.py --pending-file helper_audit_agent_9580.md --line 156 --item PENDING_ITEM_TEXT [--item ...] [--task-file TARGET_TASK.md]``
  - ``Choose `--item` values by quoting the request's words as much as possible. Do not pass `--ack-human`; agent-origin reports do not need a human acknowledgement. If there is no pending task item to add, run `omo_task_edit.py pending-marker-clear`; existing pending-item cleanup uses `omo_task_edit.py pending-replace` or `omo_task_edit.py pending-remove --evidence TEXT`. Then dispatch the task:``
  - `<snippet file="helper_audit_agent_9580.md:156-157">`
  - `(pending)`
  - `(from agent /tmp/omo-agent-messages-30033/agent_running_450901fc7c538b93789982a05ef20df3651c465ebf7f86eb641b75d6b6c5a9da.md)`
  - `</snippet>`
  - `<snippet file="/tmp/omo-agent-messages-30033/agent_running_450901fc7c538b93789982a05ef20df3651c465ebf7f86eb641b75d6b6c5a9da.md:1-4">`
  - `(sent from agent via omo_report.sh tmux=hcfg:1 time=11:08 task-file=helper_audit_agent_9580.md)`
  - `[message-sha256: 657689c723385f1d577dee5eeab617e24f457c58214e26a53e21aaa44705f552]`
  - `message:`
  - `Human requested manager-owned TODO/task cleanup.`
  - `</snippet>`
  - `<status>blocked`
  - ``<blocked_on>persistent helper-audit contact waiting for human follow-up at `wl:10.0`</blocked_on>``
  - `</status>`

- dm fyi example
  - ``Normally record pending items and remove the consumed `(pending)` marker by running:``
  - ``omo_record_pending.py --pending-file worker.md --line 11 --item PENDING_ITEM_TEXT [--item ...] [--task-file TARGET_TASK.md] --email-file manager_mail/4002.txt --ack-human``
  - ``Choose `--item` values by quoting the human's words as much as possible. Use `--ack-human` so the script emails the human after recording. If no new pending task item should be added, use `omo_task_edit.py pending-marker-clear` with `--comment`, `--clear-kind report-only|duplicate|cancelled|superseded`, `--ack-human`, and the same `--email-file` when shown above; if an active owner task already tracks it, use `--clear-kind existing-owner-item --owner-task-file TASK.md --owner-item ITEM`. Existing pending-item cleanup uses `omo_task_edit.py pending-replace` or `omo_task_edit.py pending-remove --evidence TEXT`. This message is already dispatched to the agent, this is FYI:``
  - `<snippet file="worker.md:11-12">`
  - `(pending)`
  - `(record and delegate manager_mail/4002.txt)`
  - `</snippet>`

- agent-problem routing
  - runs `omo_agent_status.py --problems-only` every `--agent-problem-interval-s` seconds, default `300`
  - scans all task owners; `OMO_MANAGER_TMUX_TARGET` only adds the main-manager self-check
  - dispatches each problem group to the row's `owner_target`, falling back to `OMO_MANAGER_TMUX_TARGET` only when no owner is known
  - detects task files with frontmatter `status: running` whose pane is `error`, `not_codex`, `ready`, or `stuck_input`
  - detects blocked persistent-role task files from frontmatter whose pane is `error`, `not_codex`, or `stuck_input`
  - detects manager pane problems when `OMO_MANAGER_TMUX_TARGET` is set
  - detects completed task files whose agents still appear open
  - detects `untracked_agent` panes when a non-`h*` tmux session contains a running, ready, errored, or stuck Codex pane that no task file owns
  - agent-problem prompts start with a direct helper instruction and do not need human acknowledgement
  - email pending refs remain `origin=human source=email action=ack-human`
  - idle checks remind each ready active agent at its own `runat` when its pending queue is nonempty

- agent-problem prompt format
  - starts with ``Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:``
  - groups each problem class under a concrete action heading
  - strips raw `role=...`, `owner_target=...`, and `unstick=...` fields from manager-facing text
  - labels pane content as `<output>...</output>` or `<input>...</input>`
  - uses only the tmux target once for `tmux:TARGET` rows
  - example:
    - ``Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:``
    - ``1 not codex; check if agent failed to launch:``
    - ``vl_langdoc_9160.md vl:32 <output>...</output>``
    - ``16 ready and not blocked; consider resuming or closing them:``
    - ``vl:11 <output>...</output>``
    - ``6 have their input being stuck; unstick or restart them:``
    - ``vl:37 <input>Manager correction: ignore the earlier Read first item for MANAGER.md.</input>``
    - ``1 not tracked in any task file; ask them what their task is, or consider resuming or closing them:``
    - ``vl:41 <output>Implemented and privately reported.</output>``
    - ``1 are marked `done` but remain open; either close the agents or correct the task status:``
    - ``task_name.md``

- scoped maintenance
  - all manager-owned worker rows are handled by the same owner-routed problem scan
  - non-blocked panes classified as `stuck_input` are submitted with Enter when the Codex status helper says the visible input is safe
  - first and second successful Enter attempts are remembered and suppressed; the third still-stuck report is sent to the owning manager
  - remembered Enter attempts are cleared when that target is no longer reported as stuck
  - unchanged `blocked_idle` rows are reported with exponential delay after each successful report: 10 minutes, 15 minutes, 22.5 minutes, and so on
  - manager self-problem rows and matching `unstuck:` rows are logged and filtered by the watcher so they are not pasted back into the manager prompt
  - `human_request` status rows are filtered from agent-problem prompts because live `(pending)` blocks are dispatched through the pending-marker path
  - manager compaction reminders say ``Unless you know the exact content of MANAGER.md, read it. Normally, don't ack human``
  - any ready active agent with pending items receives a path-opaque reminder at its own `runat`; this includes `long_running`, managers, and queues below the former size threshold
  - reminders tell the agent to use `omo_pending.py`; they do not expose task filenames, item text, `managerat`, or backing storage
  - `TODO.md` length reminders tell managers to keep only the newest 20 `previous` tasks in `TODO.md` and move older `previous` tasks to `YYYYMM/old_todos.md`
  - dirty worktree reminders name the dirty repo path, omit raw status/category fields, tell managers to let workers commit their own changes, and tell managers to commit task files themselves without routing task-file cleanup to workers
  - identical problem output is keyed by SHA-256 in process-local time-bounded delivery memory and is repeated at most once per `--agent-problem-repeat-s` seconds, default `1800`
  - digest idle delivery uses a separate human-contact clock: if `manager_digest.md` has content and the newest `manager_mail/*.txt` is at least `--digest-idle-after-s` seconds old, default `3600`, it runs `scripts/manager-digest deliver`

Human-review note: this one-hour no-human-email digest policy was added after mail `manager_mail/5125.txt`; review whether future manager instructions should make this a standing obligation or keep it as helper behavior.

Human-review note: running-agent reminders are now problem-only; review standing manager instructions if they still expect periodic healthy running-agent reminders.
