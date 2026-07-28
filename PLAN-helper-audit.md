# helper audit plan

## instructions

- talk directly to the human in chat
- use email for human-visible status that must not be missed
- stop clarifying dirty-file ownership
- focus only on reviewing helpers
- answer in one paragraph by default
- answer each separate question in one paragraph when there are multiple questions
- use the most concrete, understandable language possible
- follow long-running-autonomy
- keep this plan current
- include STUCK LOOP CHECK STOP

## task

- review all local helper scripts for reliability risks
- cover every helper-owned interface: command-line scripts, Python helpers, shell wrappers, watcher daemons, tests that define contracts, docs that agents follow, state files they create, and PATH entry points
- start with the helpers that affect manager, pending, status, mail, and tmux delivery
- explain findings and proposed fixes directly to the human
- make changes only after the human agrees to the audit direction or asks for fixes

## active state

- current migration: add `long_running` as a first-class active status for managers and human-facing interactive agents
- `long_running` status behavior: require `blocked_on` like `blocked`; suppress idle/ready close reminders; preserve error, stuck-input, malformed-state, launch-failure, task-state, and pending-item handling
- superseded temporary rule: do not key reminder suppression from `blocked_on: long-running`; migrate that intent to `status: long_running`
- authorized mail routing: ordinary addressed email goes only to the task `runat`; no manager FYI copy
- authorized manager override: `for manager` or `for a manager` at the beginning or end of active unquoted content routes to `managerat`; matching ignores case, surrounding punctuation, and edge whitespace while preserving internal spacing
- authorized marker scope: inspect the pending block plus one-level readable linked content, including linked email files; do not recursively crawl links inside attachments
- current pending ownership: workers and managers manage their own `pending_task_items` through task-file-opaque helper commands that infer the current task from tmux
- current pending reminders: deliver each idle agent's pending-item reminder to that agent, not its manager; `long_running` agents still receive pending-item reminders
- current instructions: update manager/worker/helper docs after behavior is stable, then obtain human review, commit, restart relevant watchers, and broadcast manager reread

- communication: keep chat terse; email important human-visible conclusions
- current incident: agents sent manager reports to themselves
- current human directive: `omo_report.sh` must route reports by reading the reporting task file's `managerat`, not by appending to the reporting task file
- current human directive: normal `omo_report.sh` use must not take `--root` or `--task-file`; it should read the work-log root from `.config/omo_manager/local.env`, infer the reporting task from the current tmux pane, then route to that task's `managerat`
- current human directive: report bodies must be written into helper-allocated private files through an editor or file-editing tool, not via `cat`, heredocs, or shell text injection
- active delegated implementation: report-script worker owns `omo_report.sh`, report tests, and directly required report instructions; main agent owns the human walkthrough plan and later integration/review
- current human directive: remove legacy task metadata compatibility and migrate all active task files to frontmatter
- confirmed mechanism 1: the live pending watcher was stale and still ran old `--state`, `--manager-target`, and `--manager-url` flags
- confirmed mechanism 2: `email_idle_watcher.py` retry routing used non-current task-file `runat` before `managerat`
- corrected mechanism: `omo_report.sh` now appends worker reports to the manager task file found from worker `managerat`; main-manager reports go to dated `work_manager_YYYY-MM-DD.md`
- corrected watcher rule: pending blocks already inside a manager task file route to that manager task's `runat`; worker task pending blocks route to `managerat`
- current tests: focused report, pending, email-route, status, codex status, syntax, ruff, and diff checks pass for corrected report source-routing and agent-problem handling
- current human directive: fix `omo_pending_watch` agent-problem notices so they route each problem to the owning manager, summarize actionable groups, hide noisy raw status fields, send Enter to non-blocked stuck panes before reporting, and report only after three failed unstick attempts
- current human directive: tmux sessions starting with `h` are human-owned; every other Codex-looking tmux pane is agent-owned and must be tracked by a task file, so unmatched agent panes are `untracked_agent` problems
- implemented: `omo_agent_status.py` now treats non-`h*` unmatched running, ready, errored, or stuck Codex panes as `untracked_agent`, auto-enters safe non-blocked stuck panes, leaves blocked panes untouched, and scopes raw tmux scans to the manager's tmux session when a manager target is supplied
- implemented: `omo_pending_watch.py` now formats agent-problem reports into grouped action text, suppresses first and second auto-enter notices, reports the third unresolved attempt, and forgets Enter attempts when the pane is no longer stuck
- completed: reviewer follow-up found no blockers for the agent-problem/report-routing fixes
- completed: watcher setup restarted pending, stuck, and email watchers after the helper changes

## plan

- specify `long_running` schema, transition, TODO, report-routing, status-scan, and reminder semantics before editing shared parsers
- migrate normal email routing to direct delivery and delete DM-marker branches, while retaining manager-edge override and explicit routing-failure escalation
- expose current-agent pending-item list/add/replace/remove operations without task-file arguments or task-file output; keep evidence checks for removal
- route idle pending-item reminders to each agent's `runat`, including `long_running`, and remove manager-owned worker-list reminders
- update launcher-injected worker instructions and manager instructions so agents use the opaque pending helper and managers no longer edit worker pending queues
- add realistic schema, mail, pending API, reminder, and watcher regressions; run focused suites, static checks, and reviewer loops
- preserve unrelated dirty hunks, commit owner-safe changes atomically, restart watchers through supported setup helpers, notify managers to reread, and email the human

- inventory helper entry points from PATH, `.config/bin`, `.config/helper.sh`, `.config/omo_manager`, helper-owned docs, and helper-owned test suites
- map each helper to its caller, output contract, state files, and failure behavior
- map each helper-owned state directory or file to the scripts that read and write it
- classify helpers by operational role: detect, decide, deliver, notify, remember, or document
- audit message tracking and sending first because this is the path that records, deduplicates, routes, and delivers helper messages
- audit manager status flow after that because stale or wrong status misleads later decisions
- audit pending marker flow after that because duplicate or stale markers create repeated work
- audit mail flow after that because noisy or wrongly routed messages hide real failures
- audit tmux delivery after that because wrong target delivery makes automation act on the wrong session
- audit task launch and watcher helpers after the core paths because they compose the earlier helpers
- audit docs and tests only where they define expected behavior or reveal gaps
- rank findings by user impact, reproducibility, and blast radius
- propose narrow fixes with tests only when a defect has a clear local cause

## helper realm map

- human contact helpers: `helper.sh/email_me.py`, `helper.sh/vb-speak.sh`, `helper.sh/natural-syntax-ls.sh`
- message and pending helpers: `omo_pending_watch.py`, `omo_pending_digest.py`, `omo_dispatch.sh`, `omo_report.sh`
- status and stuck helpers: `omo_agent_status.py`, `omo_codex_status.py`, `omo_pending_watch.py`, `omo_worktree_check.py`
- tmux and Codex delivery helpers: `omo_tmux_send.py`, `omo_codex_stop.py`, `omo_codex_compact_when_idle.py`, `omo_manager_restart.sh`
- task lifecycle helpers: `omo_task.py`, `omo_task_status.py`, `omo_spawn_session.py`, `omo_project_registry.py`
- watcher process helpers: `omo_manager_setup_watchers.sh`, `omo_manager_watchdog.sh`, `email_idle_watcher.py`
- mail and digest helpers: `omo_email_subject.py`, `omo_digest_queue.py`, `omo_manager_mail_compress.py`
- verification helpers: `omo_quiet_checks.sh`, `omo_manager_quiet_check.sh`, `omo_triage_report.py`
- cost/history helpers: `omo_codex_cost.py`, `omo_oc_history.py`
- OpenCode legacy helpers: `opencode_auth_switch.py`, `opencode_auth_rotation_dryrun.py`, `opencode_quota_profile_watch.py`, `opencode_rotation_quiet_check.py`, `pcodx`
- domain-specific helpers to prune or isolate: `omo_vl_experiment_preflight.py`
- helper docs and tests: `omo_manager/docs/**`, `omo_manager/tests/**`, `WORKER_DEFAULTS.md`, `MANAGER_HELPERS.md`, `VL_WORKER_DEFAULTS.md`, work-log `MANAGER.md`

## human walkthrough plan

- review exactly one helper surface at a time
- for each helper, start with purpose, caller, inputs, outputs, state, and failure behavior
- point to exact files and line ranges before discussing behavior
- answer the human's immediate question first, then list only the next concrete audit question
- avoid mixing implementation, design, and review unless the human asks to fix immediately
- keep the current helper open until these are clear:
  - why the helper exists
  - what data it reads and writes
  - how it avoids duplicate or stale work
  - how it decides routing or target ownership
  - how it proves delivery or completion
  - how it fails and who is notified
  - what tests lock the behavior
- move to the next helper only after the human agrees or no open reliability question remains

## checklist

- [ ] `long_running` requires `blocked_on` across schema, lifecycle, launcher, docs, and tests
- [ ] direct-by-default mail routing and manager markers
- [x] task-file-opaque agent pending-item commands
- [x] agent-targeted idle pending-item reminders
- [x] manager and worker instruction migration
- [ ] focused integration tests and reviewer loops
- [ ] owner-safe commits and watcher restart
- [ ] manager broadcast and human email

- [ ] helper inventory
- [ ] PATH and wrapper entry points
- [ ] helper-owned state files
- [ ] helper-owned docs
- [ ] message tracking and sending
  - accepted assumption: Markdown pending files are generally append-only or block-swap-only, so relative pending line positions should not shift often enough to require stable message IDs now
  - changed `omo_pending_watch.py` pending/problem `seen` tracking from disk-backed state to process-local time-based cache
  - removed live `pending-seen.tsv`
  - changed `omo_pending_watch.py` to include pending-line context, source line ranges, a remove-marker instruction, and 2000-char start/end truncation in deliveries
  - changed `omo_pending_watch.py` to attach referenced file content, including pointed email content when a pending line references manager mail and absolute agent report paths
  - changed `omo_pending_watch.py` DM handling so email messages ending in `DM` route directly to the worker with an FYI copy to the manager
  - kept compact `(from agent ...)` source markers as the preferred agent pending-block format
  - changed `omo_pending_watch.py` pending routing so normal pending goes to `managerat` and only DM delivery goes to `runat`
  - reviewer loop complete for first DM/direct-delivery pass
  - reviewer follow-up found frontmatter relaunch, invalid runat, email routing, pending stale-suppression, and stale-doc issues
  - active fix: reviewer follow-up issues patched with focused tests and docs
  - reviewed: `omo_pending_watch.py` source classification; agent markers win over email markers, email markers require human ack, unmarked pending blocks require human ack, and generated routed email blocks place `(manager routed: ...)` immediately after `(pending)` so stale routed blocks are skipped
  - finding: `omo_pending_watch.py` delivery failure handling treats nonzero push exits as retryable but does not catch `subprocess.run` launch exceptions in `push_marker_text` or `push_manager_text`, so a PATH/spawn failure can crash the watcher instead of leaving the marker unresolved for retry
  - reviewed fix: push helpers catch launch exceptions, log them, return failure, and keep unresolved markers retryable
  - reviewed fix: top-level pending-watcher crash guard emails the human on unexpected process crash, passes a sender tmux target when available, and re-raises for supervisor restart
  - pending review: `omo_pending_watch.py` watcher loop and restart behavior, including bounded in-memory `seen`, full rescans, mtime polling, and restart duplicates
  - finding: restart loses process-local `seen`; direct `omo_tmux_send.py` delivery validates pending marker freshness but does not durably mark a successful pending delivery as routed, so unchanged unannotated pending blocks can be delivered again after watcher restart
  - pending review: `omo_pending_watch.py` docs/tests gaps after the current script review is complete
  - active incident fix: `email_idle_watcher.py` retry routing changed so worker task files route to `managerat`, not `runat`
  - active incident check: live pending watcher command no longer includes old pending-watcher routing flags
  - active incident review: reviewer found no blocker for the self-route fix
- [ ] status helpers
  - first pass complete: producer, aggregator, watcher consumer, and representative tests inspected
  - deeper pass pending: exact false-positive cases, live target ownership, and output-noise policy
  - reviewed fix: agent-problem report format and ownership routing from `omo_agent_status.py` output through `omo_pending_watch.py`
  - reviewed fix: unmatched non-human Codex panes are reported as `untracked_agent`; the manager instruction says to ask the agent what their task is or consider closing them
  - reviewed fix: safe non-blocked stuck panes receive Enter before reporting; first and second attempts are suppressed, the third unresolved attempt is reported, and attempt memory is cleared when the pane is no longer stuck
  - reviewed fix: still-stuck panes keep Enter-attempt memory even when the latest status is `not_safe:*` or another non-`sent_enter` result
- [ ] pending helpers
- [ ] mail helpers
- [ ] tmux delivery helpers
- [ ] report helpers
  - active worker: remove normal-use `--root` and `--task-file` from `omo_report.sh`
  - active worker: keep work-log root from `local.env`/`OMO_WORK_LOGS_ROOT`
  - active worker: infer task from current tmux target and task frontmatter `runat`
  - active worker: route report by inferred task `managerat`
  - active worker: update tests and report instructions to use `REPORT_FILE=$(omo_report.sh --alloc-message-file)`, editor/file-editing writes, and `omo_report.sh --status STATUS --message-file "$REPORT_FILE"`
- [ ] task launch helpers
  - active fix: `omo_task.py` prelaunch hook from `manager_prelaunch_hook_note_9758.md`
  - problem: `omo_task.py` hard-codes VL experiment preflight flags, automatic VL filename gating, provider policy, Verus/vlh setup, and worker env injection
  - requested direction: replace command-plus-args prelaunch design with one generic launcher script file that the launched shell sources before starting the managed agent
  - source-script contract to implement: `omo_task.py` validates a readable script path, sources it in the same shell that launches Codex/pcodx, lets the script fail the launch by returning nonzero, and keeps manager code ignorant of project-specific env names such as `VLH`, `VERUS`, and provider policy
  - expected cleanup: remove or deprecate `--vl-experiment-preflight`, `--vl-preflight-vlh`, `--vl-preflight-verus`, `--vl-preflight-artifact-root`, automatic `vl_*_exp_*`/`vl_*_rerun_*` preflight gating, `VL_EXPERIMENT_PREFLIGHT`, `vl_preflight_env`, and `run_vl_experiment_preflight`
  - tests to update: `omo_manager/tests/test_omo_task.py` should cover parsing the generic prelaunch script flag, dry-run launch command, source-script ordering before Codex, source-script path validation before mutation, and removal of the VL-specific preflight behavior
  - docs to update if implementation lands: `omo_manager/docs/routing/task-launch.md` and `omo_manager/VL_WORKER_DEFAULTS.md`
  - reviewed design: `omo_manager/docs/routing/task-file-frontmatter.md` is committed as `a331807`
  - frontmatter schema: `version`, `status`, conditional `blocked_on`, `runat`, `tool`, `managerat`, `is_manager`, and `pending_task_items`
  - frontmatter constraint: all task files except main manager task files must have frontmatter; helpers must treat frontmatter as source of truth
  - migration scope: active task files only; parse bottom `runat:` and `managerat:`, parse latest non-pending status marker, move open pending task bullets into `pending_task_items`, leave body as is
  - active implementation: script worker is completing `omo_task.py`, `omo_agent_status.py`, `omo_pending_watch.py`, append-only mail/report helpers only if needed, and focused tests
  - reviewed: MANAGER.md frontmatter instruction patch had reviewer issues and was tightened so `status: blocked` requires a concrete blocker
  - migration done: task-file migration worker added frontmatter to 32 active task files using `pending_task_items`
  - migration blockers: 10 active task files lacked enough valid legacy metadata, e.g. no blocked reason, no managerat, managerat equals runat, or human runat/tool mismatch
  - new directive: no backwards compatibility; all active task files must be migrated instead of keeping helper fallback paths
  - active definition: `TODO.md` sections `current`, `human pending`, and `low priority`
  - active implementation split: task-file migration worker owns `/ssd1/sichangheagent/work_logs` active task files; helper-code worker owns `.config/omo_manager` legacy metadata fallback removal
- [ ] watcher helpers
- [ ] docs and instruction contracts
- [ ] test coverage gaps
- [ ] ranked findings
- [ ] fix proposals

## STUCK

- if progress stalls, state the exact unknown, the attempted evidence, and the smallest next test

## LOOP

- after each audit section, update this plan and continue to the next unchecked section

## CHECK

- validate findings against real call paths, tests, and representative commands

## STOP

- when all checklist items are done, remove this plan file if no longer useful and summarize remaining risks
