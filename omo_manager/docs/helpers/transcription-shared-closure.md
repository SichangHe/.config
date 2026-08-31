# transcription shared-target closure

goal

- record the already-delivered transcription answer without resending it
- close only the completed transcription task
- preserve the distinct active research manager that shares `wl:32`

mail adoption

- `omo_completion_mail_adopt.py`
  - reads the dedicated agent Gmail account only
  - selects All Mail and Sent read-only
  - requires one exact Message-ID in both mailboxes
  - binds Gmail identity, internal date, raw MIME, body, sender, recipient, subject, reply headers, and the complete ordered thread
  - binds the exact blocked v1 worker bytes and complete ordered queue
  - writes one new 0600 receipt in an existing private directory
  - sends no mail and changes no mailbox or lifecycle file

shared-target closure

- `omo_shared_task_done.py`
  - has no mail or tmux API
  - consumes the exact adoption receipt
  - binds the source task, TODO, and distinct owner bytes
  - requires exactly two active `wl:32` owners before mutation
    - `transcription_sw.md`
    - `memory_research_mgr.md`
  - locks root membership, every task record, TODO, and the shared target
  - clears only the receipt-bound queue
  - changes only the transcription status and blocker
  - moves only the transcription TODO row from `current` to `previous`
  - requires the research manager to remain the sole active `wl:32` owner
  - never reads, signals, sends input to, or stops the pane

failure policy

- reject missing or ambiguous Sent evidence
- reject any mail, thread, task, queue, target, type, status, manager, TODO, receipt, or ownership drift
- reject missing, additional, malformed, or inactive shared-target owners
- reject concurrent supported lifecycle mutation through common locks and final byte checks
- never convert uncertain delivery into permission to resend

operation

- obtain separate approval for the fully digest-bound commands
- run adoption first
- verify the immutable receipt digest
- run shared-target closure with that digest
- create fresh bindings after any rejection
