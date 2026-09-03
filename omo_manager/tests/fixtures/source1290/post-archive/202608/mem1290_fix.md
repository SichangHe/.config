---
version: v1.0.0
status: done
runat: vldr:1
tool: codex
managerat: vldr:5
is_manager: false
pending_task_items: []
---
<manager_delegation from="vldr:0">
Implement exactly one bounded helper change for Source-1290 lifecycle recovery.

- Add an evidence-bound reconciliation for an interrupted `done_close_in_progress` task at an authenticated ordinary shell with empty queue. It must either finish carrier-only done or safely transition into the existing `done_close_failed` recovery contract. Preserve task evidence, completed audit, authority custody, mail/no-duplicate-email invariants, and fail closed on pane/task identity or evidence drift. Add focused deterministic tests. Own only the minimum `.config/omo_manager` helper, tests, and directly corresponding documentation paths; do not apply production, touch any Source-1290 task record, pane, TODO, mailbox, audit, duplicate carrier, transcription state, or Human contact. Obtain a distinct complete-diff reviewer PASS before promotion, fix reasonable issues and rereview, then commit and push only owned paths. Report high-level result and process feedback privately with an allocated `omo_report.sh` file; do not print only in the TUI or use manual routing flags.
</manager_delegation>
(verified removed pending item: Implemented in commit a7f815d on pushed branch fix/source1290-done-close-reconcile. Six owned paths only; 290 relevant unittests, ruff, py_compile, and diff checks passed; distinct complete-diff reviewer /root/source1290_complete_diff_final_review returned PASS.)
(verified removed pending item: Completed in pushed commit 317d8f3 on fix/source1290-done-close-reconcile. The deterministic test models successful close return followed by pre-note process death, a second interrupted retry, and final idempotent completion; late evidence drift and target reappearance fail closed. 292 relevant tests pass and fresh distinct complete-diff reviewer /root/source1290_postkill_complete_diff_review returned PASS.)
(verified removed pending item: Completed and pushed as frozen commit 2d9f2489876158d8b24263184922f23afd2a8368 on fix/source1290-done-close-reconcile. Sole ownership is rechecked after shell authentication and in the final live-close/absent-finish gate; deterministic duplicate-owner regressions cover both placements. 294 relevant tests and focused static checks pass; fresh reviewer /root/source1290_owner_drift_complete_review returned PASS.)
(verified removed pending item: Owner private done report agent_done_f0f2bcd7453ec3734a3a2a2479df98c1858dfa4ed863e180a17bbe02c3bd2be3 confirms exact helper succeeded once with durable Message-ID.)

(manager closed Codex agent 08-31 22:08 PDT; tmux target `vldr:1`; Codex session id not found in captured tmux output.)
