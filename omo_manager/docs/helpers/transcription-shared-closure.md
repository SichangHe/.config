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
  - rejects the adoption receipt as direct closure authority
  - consumes a separate recovery receipt bound to the preserved incident receipt and fresh lifecycle bytes
  - requires a third owner-read-only approval artifact bound to the acknowledged packet digest, both exact actions, both receipts, and all lifecycle digests
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

incident recovery

- `omo_incident_receipt_recover.py`
  - reads the unauthorized but valid adoption receipt as incident evidence
  - preserves its exact path, bytes, inode, mode, owner, size, and modification time
  - rebinds the exact task, TODO, protected owner, complete queue, and active shared-target membership
  - writes one exclusive 0600 recovery receipt in a different existing 0700 directory
  - states that execution authority is not contained in either receipt
  - has no mail, tmux, or production lifecycle write path
- the recovery receipt does not authorize execution by itself
- separate explicit production approval must create an owner-read-only artifact after acknowledging the immutable packet digest
- that artifact authorizes exactly one recovery-evidence creation and one shared-task closure with the bound bytes
- the reviewed implementation binds no trusted approval digest, so same-UID files cannot self-authorize execution
- authenticated Human approval requires a later reviewed code change that binds its exact immutable evidence digest

operation

- obtain separate approval for the fully digest-bound commands
- first deliver only the immutable packet digest and obtain acknowledgment
- then provide the packet; receipt does not grant execution authority
- for an unauthorized adoption receipt, preserve it and run recovery-evidence creation only after the separate approval artifact exists
- verify both immutable receipt digests
- run shared-target closure only after the same approval explicitly covers it
- create fresh bindings after any rejection
