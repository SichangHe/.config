# pending Sent-mail recovery

(authored by agents unless marked 🧑)

`omo_pending.py recover-source1990-pangram` is the only public no-send recovery command.

- exact bindings
  - expected task bytes and ordered queue
  - full ordered Human item set and purpose digest
  - one verified Sent message: RFC Message-ID, decoded subject digest, and the exact canonical pending-notice body digest
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
  - no-contact policy is still evaluated; recovery merely permits adoption of evidence that was already delivered
  - a message can bind one recovery only; its marker includes the transition key, so a second request or replay with different bindings stops before mutation
- Source-1990 Pangram adapter
  - `recover-source1990-pangram` is one incident-specific direct-Human authority adapter
  - it verifies immutable Source-1990 bytes plus the literal task, owner/manager, ordered items, queue/task/purpose digests, Sent message, claims, and churn proof
  - its current task binding additionally proves the exact authenticated post-authority Git transition from the earlier custody task; any other body drift stops before mutation
  - it does not infer authority from the email thread and accepts no other recovery
