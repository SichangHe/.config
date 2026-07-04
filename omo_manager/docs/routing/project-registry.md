# project registry helper

`omo_project_registry.py` maintains the main-manager-owned project registry and validates compact project summaries for submanager routing.

It writes `manager_projects.md`, leaving project-owned state and worker task files untouched. `upsert` creates or updates one project row with `name`, `status`, `submanager-target`, `submanager-task`, `summary-file`, `goal`, `blocker`, `last-heartbeat`, and `next-checkpoint`.

`check` validates the registry and each referenced summary file against the contract in `submanager-orchestration.md`. The first pilot is VL: `vl_supervisor_current_7404.md` on `vl:9` owns detailed VL project routing, while the main manager consumes `vl_summary_for_main.md` first and reads detailed VL state only for audit, escalation, or stale heartbeat.

Human-review note: this is helper documentation and pilot state, not an approved standing change to `MANAGER.md`.
