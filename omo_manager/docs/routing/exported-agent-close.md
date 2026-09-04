# exported agent close

`omo_exported_agent_close.py` closes task metadata only after Source-1398 queue custody was exported and verified.

- supported shapes
  - absent `long_running` manager indexed in `previous`
  - absent blocked worker with no TODO row
  - blocked worker indexed in `current` or `human pending`, sharing a live target with one exact protected sibling
  - blocked manager sharing an absent target name with one exact protected active sibling
- prepare
  - binds exact task, TODO, export, Human-authority envelope, target, queue, sibling, pane, file, and ancestor state
  - writes one owner-private immutable packet without changing lifecycle state
- execute
  - requires an authenticated exact PASS from a different source and destination target
  - revalidates every bound input under lifecycle locks
  - publishes a recoverable prepared audit before mutation
  - exchanges task and TODO bytes through held parent descriptors and rejects final-window substitution
  - restores the exact prior leaf after a safe exchange failure or preserves it under the transaction name and reports an indeterminate state
  - clears the task queue, marks the task done, and records targetless `previous` custody
  - leaves tmux unchanged
  - rolls TODO back if the task write fails
- exclusions
  - shared-target managers whose target is live or whose exact protected sibling is not uniquely bound
  - malformed or pending-marker tasks
  - changed exports, queues, TODO rows, targets, panes, or sibling ownership
