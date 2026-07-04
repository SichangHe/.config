# submanager orchestration

status
- helper contract, not standing manager policy
- human review required before copying these rules into `MANAGER.md`

ownership
- main manager owns `manager_projects.md`
  - project registry
  - human-facing routing
  - stale summary audit
- project submanager owns project state
  - project brief/state/log files
  - worker routing for that project
  - compact summary for main
- worker owns task files and reports

main registry
- file: `manager_projects.md`
- helper: `omo_project_registry.py`
- required fields
  - `name`
  - `status`
  - `submanager-target`
  - `submanager-task`
  - `summary-file`
  - `goal`
  - `blocker`
  - `last-heartbeat`
  - `next-checkpoint`

summary contract
- one short Markdown file per project
- consumed by main manager during normal operation
- required fields
  - `project`
  - `status`
  - `owner`
  - `goal`
  - `state`
  - `next-action`
  - `blocker`
  - `risk`
  - `last-heartbeat`
  - `next-checkpoint`
  - `evidence`

VL pilot
- project id: `vl`
- first submanager: live `vl_supervisor_current_7404.md` on `vl:9`
- summary file: `vl_summary_for_main.md`
- main-manager consumption rule
  - read the summary first
  - inspect detailed VL task files only for audit, escalation, or stale heartbeat
- current high-level blockers
  - C and D no-helper launches need a compliant isolated-container agent runtime and auth path
  - F multi-capability recovery prep is the active safe VL lane

checks
- enforced by helper
  - registry and summary files stay under the work-log root
  - registry `submanager-target` matches summary `owner`
  - registry `submanager-task` exists under the work-log root
- informational only
  - summary `evidence` points to supporting files or task names, but is not recursively validated
- initialize registry
  - `omo_project_registry.py --root /ssd1/sichangheagent/work_logs init`
- update a project row
  - `omo_project_registry.py --root /ssd1/sichangheagent/work_logs upsert --project-id vl ...`
- validate registry and project summaries
  - `omo_project_registry.py --root /ssd1/sichangheagent/work_logs check`
