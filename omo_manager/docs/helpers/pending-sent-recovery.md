# pending Sent-mail recovery

(authored by agents unless marked 🧑)

Public no-send recovery commands are incident-specific. They accept only their registered incident inputs and never invoke the email sender.

- exact bindings
  - expected task bytes and ordered queue
  - full ordered Human item set and purpose digest
  - one verified Sent message: RFC Message-ID, decoded subject digest, and the exact incident-bound body digest
  - unused completion claim, authorization bytes, target, task bytes, and semantic key
  - exact Git commit/blob/diff proof when manager ownership changed
- durable behavior
  - fsyncs newly created state directories before recording recovery state
  - records the Message-ID-to-transition binding before any tombstone or transition record
  - tombstones every retired claim before writing the task
  - records `prepared`, writes and fsyncs the exact queue mutation, then records `committed`
  - a repeat replays only the matching prepared transaction and never sends mail
- limits
  - verification reads Gmail Sent Mail and requires exactly one message from the configured agent address to the configured Human address
  - new-email planning still obeys no-contact policy
  - a no-send recovery plan can inspect exact pre-existing Sent evidence despite that policy, but has `send_allowed=false` and the sender rejects it
  - a message can bind one recovery only; its marker includes the transition key, so a second request or replay with different bindings stops before mutation
- Source-1990 Pangram adapter
  - `recover-source1990-pangram` is one incident-specific direct-Human authority adapter
  - it verifies immutable Source-1990 bytes plus the literal task, owner/manager, ordered items, queue/task/purpose digests, Sent message, claims, and churn proof
  - its current task binding additionally proves the exact authenticated post-authority Git transition from the earlier custody task; any other body drift stops before mutation
  - it does not infer authority from the email thread and accepts no other recovery
- Source-1970 evaluation adapter
  - `recover-source1970-eval` takes no incident parameters
  - it binds the exact Source-1970 file and current `eval_sufficiency.md` bytes, blocked status, owner, manager, five-item live queue, missing sixth body record, and four-commit task lineage
  - it authenticates all five delivered creation claims without changing them and retires only the unused missing-item add claim and unused removal claim
  - its one global Message-ID record binds the five-item queue mutation and ordered six-item resolution before either claim or task mutation; later delivery checks accept it only through the exact canonical committed transition
  - it records `prepared`, removes the five live items, appends the six-item delivered-answer evidence, fsyncs the empty queue, and records `committed`
  - a crash after the task replacement replays only that exact prepared transition; drift or Message-ID reuse fails closed
- Source-1994 plot adapter
  - `recover-source1994-plot` takes no incident parameters and never invokes the sender
  - it binds the exact authority file, owner task bytes, empty ordered queue, blocked lifecycle, authenticated owner/manager, acknowledgement and reviewed-result Sent messages, result commit on `main`, and sole failed add claim
  - it atomically records all six exact Human items, durably preserves their text, reconciles only the five completed figure changes, and leaves the review-before-paper-integration item open
  - it preserves the earlier Source-1977 reconciliation and every unrelated claim; any task, queue, source, claim, commit, or Sent-message drift fails before task mutation
- Source-2003 Pangram add adapter
  - `recover-source2003-pangram` takes no incident parameters and never invokes the sender
  - it binds the exact queue-empty `src1964_pangram.md` bytes, blocked lifecycle, `dw:15` owner, `dw:60` manager, compressed-source pointer, and owner-private authenticated report replay
  - it authenticates the one existing acknowledgement by Message-ID and exact subject/body digests, plus the three distinct unused item-specific add claims and authorizations
  - it atomically records the three exact items with Human provenance, preserves every unrelated task byte and claim, and supports only its exact prepared/committed replay
  - task, queue, source replay, acknowledgement, or claim drift fails before task mutation
- watcher Pangram reviewed-Sent adapter
  - `recover-watcher-pangram-reviewed-sent` takes no incident parameters and never invokes the sender
  - it binds only the current `watcher_repair.md` bytes, `config:35` owner, `config:39` manager, complete ordered queue, and its exact three-item subset
  - it requires the reviewed Sent Message-ID, subject digest, and body digest; it does not accept a canonical-notice body substitute or a completion claim
  - it removes only that subset, preserves every other queue item, and records a prepared/committed Message-ID-bound transaction for exact replay
- mailbox-compression reviewed-Sent adapter
  - `recover-mail-compress-reviewed-sent` takes no incident parameters and never invokes the sender
  - it binds the stopped `mail_compress_1984.md` bytes, `config:44` owner, `config:27` manager, and full ordered queue after the first Human item was already removed
  - it authenticates the original reviewed final report for the instruction-diagnosis item and the existing deletion notice for the streaming-batches item
  - it preserves both delivered claims and both agent-authored watcher notices byte-for-byte, retires all three earlier failed claims, and removes only the remaining two Human items
  - task, queue, claim, authorization, or Sent-message drift is rejected before task mutation
