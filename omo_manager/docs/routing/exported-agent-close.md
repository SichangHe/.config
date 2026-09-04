# exported agent close

`omo_exported_agent_close.py` closes task metadata only after Source-1398 queue custody was exported and verified.

- supported shapes
  - absent `long_running` manager indexed in `current`
  - absent `long_running` manager indexed in `previous`
  - absent blocked worker with no TODO row
  - blocked worker indexed in `current` or `human pending`, sharing a live target with one exact protected sibling
  - blocked manager sharing an absent target name with one exact protected active sibling
  - blocked queue-empty live manager whose exact bound children are all terminal
    - requires an independent authenticated recovery record, its exact failed asynchronous sender metadata/status, and a freshly unchanged fatal-error state
  - ready `long_running` non-Human manager with an open exported queue, one current TODO row, singleton target ownership, and only terminal absent direct children
    - authenticates the exact Source-1402 document-all-work and close-all-agents instruction
    - gracefully closes only the bound pane and parks the unchanged queue as blocked, targetless low-priority custody
- prepare
  - binds exact task, TODO, export, Human-authority envelope, target, queue, sibling, pane, file, and ancestor state
  - binds the live-manager stop evidence and terminal sender files
  - writes one owner-private immutable packet without changing lifecycle state
  - never signals a live manager during preparation or review
- execute
  - requires an authenticated exact PASS from a different source and destination target
  - revalidates every bound input under lifecycle locks
  - publishes a recoverable prepared audit before mutation
  - exchanges task and TODO bytes through held parent descriptors and rejects final-window substitution
  - restores the exact prior leaf after a safe exchange failure or preserves it under the transaction name and reports an indeterminate state
  - clears the task queue, marks the task done, and records targetless `previous` custody
    - except the exported-park shape, which preserves the queue, marks the task blocked with the immutable export as its blocker, and records targetless `low priority` custody
  - leaves tmux unchanged
    - except the live-manager shape, whose authenticated execute step closes only the bound pane before metadata closure
  - rolls TODO back if the task write fails
- exclusions
  - Human-owned `h*` targets and absence-probe errors or ambiguity
  - shared-target managers whose target is live or whose exact protected sibling is not uniquely bound
  - live managers with an open queue, a nonterminal or changed child, or changed pane, process, or session identity
  - live managers without a terminal failed sender and independent non-destructive-recovery evidence for the unchanged fatal error
    - exported-park managers that are running, errored, not Codex, shared, malformed, or have any live/nonterminal direct child
  - malformed or pending-marker tasks
  - changed exports, queues, TODO rows, targets, panes, or sibling ownership
