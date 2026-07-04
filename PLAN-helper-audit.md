# helper audit plan

## instructions

- talk directly to the human in chat
- do not use email for this task
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

## plan

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

## checklist

- [ ] helper inventory
- [ ] PATH and wrapper entry points
- [ ] helper-owned state files
- [ ] helper-owned docs
- [ ] message tracking and sending
  - accepted assumption: Markdown pending files are generally append-only or block-swap-only, so relative pending line positions should not shift often enough to require stable message IDs now
  - changed `omo_pending_watch.py` pending/problem `seen` tracking from disk-backed state to process-local bounded LRU cache
  - removed live `pending-seen.tsv`
  - changed `omo_pending_watch.py` to include pending-line context and truncated file-tail context in worker deliveries
  - changed `omo_pending_watch.py` to attach pointed email content when a pending line references manager mail
  - changed `omo_pending_watch.py` DM handling so email messages ending in `DM` route directly to the worker with an FYI copy to the manager
  - kept compact `(from agent ...)` source markers as the preferred agent pending-block format
  - changed `omo_pending_watch.py` owner target routing to use the latest valid `runat:` or `managerat:` before the pending line and ignore later stale directives
  - reviewer loop complete for first DM/direct-delivery pass
  - reviewer follow-up found frontmatter relaunch, invalid runat, email routing, pending stale-suppression, and stale-doc issues
  - active fix: reviewer follow-up issues patched with focused tests and docs
  - pending review: `omo_pending_watch.py` source classification, including short source marker parsing and stale-marker behavior
  - pending review: `omo_pending_watch.py` delivery failure behavior, including failed tmux sends, missing targets, and partial manager/worker delivery
  - pending review: `omo_pending_watch.py` watcher loop and restart behavior, including bounded in-memory `seen`, full rescans, mtime polling, and restart duplicates
  - pending review: `omo_pending_watch.py` docs/tests gaps after the current script review is complete
- [ ] status helpers
  - first pass complete: producer, aggregator, watcher consumer, and representative tests inspected
  - deeper pass pending: exact false-positive cases, live target ownership, and output-noise policy
- [ ] pending helpers
- [ ] mail helpers
- [ ] tmux delivery helpers
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
