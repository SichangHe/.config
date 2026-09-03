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
  - handles only the canonical non-manager authority carrier
  - authenticates the exact immutable cancellation audit and its original carrier digest
  - bridges that digest only to the exact frozen post-archive carrier image
  - authenticates the archived mailbox source through owner-controlled directories and requires its root predecessor absent
  - binds current TODO and archive TODO to caller-supplied digests
    - requires exact frozen memory, transcription, duplicate-carrier, and archived-helper bytes
    - requires archived TODO custody and absent root predecessors
    - requires the duplicate carrier blocked under its exact Human-pending custody
    - requires both preserved helper records done, their TODO rows previous, and their retired targets unowned
  - requires exact carrier, TODO, completed-audit, mailbox-source, numeric-pane, Codex-session, terminal-report evidence, and sole carrier-target ownership before and after shell authentication and at the final finish gate
  - initially requires exact `done_close_in_progress`; an intent retry requires its canonical `done_close_failed`; both require an empty queue, one `current` row, sole target ownership, and one unchanged Source-1290 Human envelope
  - locks root membership, every Markdown record, both TODO indexes, the carrier target, mailbox source, completed audit, and close intent
  - authenticates the unchanged ordinary shell before any write
  - records the canonical `done_close_failed: ... status=not_codex` blocker, fsyncs its bound carrier directory, then durably prepares an adjacent owner-private close intent before pane mutation
  - binds that intent to the authenticated terminal-capture digest, carrier, TODO, completed audit, Source-1290 authority, session, and terminal evidence before closing the exact numeric pane
    - the pane kill and the accepted ordinary-shell command set share one tmux-server identity predicate, so a post-authentication command change cannot close the pane
    - a process interruption before the kill leaves the exact pane live for reauthentication; a successful close followed by a pre-note interruption leaves the intent needed to finish without inferring closure from bare absence
    - an absent-pane finish rechecks all bound evidence and that the symbolic target did not reappear before close-note bookkeeping
    - an ordinary close failure remains in the existing live-pane `done_close_failed` recovery contract
  - on a successful exact-pane close, records the close UUID, moves only the carrier row to `previous`, and sets only the carrier `done`
  - never calls completion-mail or mailbox APIs and never mutates the completed audit, Human source, memory, transcription, duplicate carrier, or archived helper records

- reject task, TODO, audit, mailbox, authority-envelope, membership, owner, pane, session, terminal, or shell drift before close
- reject pane absence without a close intent, or an intent bound to different task, audit, authority, session, terminal, or capture evidence
- preserve the failed-close handoff if the authenticated close does not complete
- preserve a close note and `done_close_bookkeeping_failed` retry state if post-close lifecycle bookkeeping fails
