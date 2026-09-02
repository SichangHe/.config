# watchers

- `pending-watch.md`
  - deliver Markdown `(pending)` markers to the owning target
  - run conservative maintenance checks without delaying marker delivery
- `external-task-registration.md`
  - register one unchanged external-repository task for status and watcher discovery
  - bind task, TODO, ownership, lifecycle, and registry state in an append-only receipt
- `../agent-audit.md`
  - opt-in, bounded transcript audit supervisor
  - disabled unless `OMO_MANAGER_ENABLE_AGENT_AUDIT=true`; state, pid, and log files stay under `OMO_MANAGER_STATE_DIR`
