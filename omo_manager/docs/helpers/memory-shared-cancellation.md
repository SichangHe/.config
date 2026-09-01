# memory shared-target cancellation

goal

- cancel only `memory_research_mgr.md` under exact Human Source-1290
- preserve `transcription_sw.md` and shared target `wl:32`

contract

- `omo_task_status.py --cancel-shared-target`
  - authenticates exact source and sole authoritative envelope
  - binds complete memory, transcription, and TODO bytes
  - requires exactly those two active `wl:32` records
  - preserves the cancelled ordered queue in a new owner-private audit
  - clears only the memory queue and blocker
  - sets only memory status to `done`
  - moves only the memory TODO row to `previous`
  - accesses no tmux or mail API

failure policy

- reject wrong authority, task, target, protected task, or owner set
- reject task, queue, TODO, membership, or protected-record drift
- reject active descendants, malformed rows, unsafe audit paths, and rollback failure
- never infer cancellation from an email receipt or task prose

interrupted carrier closure

- `omo_source1290_done_reconcile.py`
  - handles only the non-manager task named by the completed cancellation audit as its authority envelope
  - requires exact carrier, TODO, completed-audit, mailbox-source, numeric-pane, Codex-session, and terminal-report evidence
  - binds the completed audit's authority-envelope digest to the carrier through only its canonical `running` or `long_running` to done-close status transition
  - initially requires exact `done_close_in_progress`; an intent retry requires its canonical `done_close_failed`; both require an empty queue, one `current` row, sole target ownership, and one unchanged Source-1290 Human envelope
  - locks root membership, every Markdown record, TODO, the carrier target, mailbox source, and completed audit
  - authenticates the unchanged ordinary shell before any write
  - records the canonical `done_close_failed: ... status=not_codex` blocker, fsyncs its bound carrier directory, then durably prepares an adjacent owner-private close intent before pane mutation
  - binds that intent to the authenticated terminal-capture digest, carrier, TODO, completed audit, Source-1290 authority, session, and terminal evidence before closing the exact numeric pane
    - the pane kill and the accepted ordinary-shell command set share one tmux-server identity predicate, so a post-authentication command change cannot close the pane
    - a process interruption before the kill leaves the exact pane live for reauthentication; an interruption after the kill leaves the intent needed to finish without inferring closure from bare absence
    - an ordinary close failure remains in the existing live-pane `done_close_failed` recovery contract
  - on a successful exact-pane close, records the close UUID, moves only the carrier row to `previous`, and sets only the carrier `done`
  - never calls completion-mail or mailbox APIs and never mutates the completed audit, Human source, memory record, transcription record, or other authority carrier

- reject task, TODO, audit, mailbox, authority-envelope, membership, owner, pane, session, terminal, or shell drift before close
- reject pane absence without a close intent, or an intent bound to different task, audit, authority, session, terminal, or capture evidence
- preserve the failed-close handoff if the authenticated close does not complete
- preserve a close note and `done_close_bookkeeping_failed` retry state if post-close lifecycle bookkeeping fails
