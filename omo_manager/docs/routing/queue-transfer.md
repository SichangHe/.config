# queue transfer

(authored by agents unless marked 🧑)

- purpose
  - move one complete ordered v1 `pending_task_items` queue to existing task owners
  - bind every source item to one transfer or terminal disposition
  - leave a committed manifest and one owner-private receipt per destination
- safety model
  - `prepare` checks exact request, task, and ordered-queue digests without editing tasks
  - the manifest retains the exact request bytes and reconstructs every task/disposition binding from them
  - `apply` holds the membership, every affected owner-target lock, and every affected file lock
  - source ownership is cleared before any destination gains work, so no item has two task owners
  - the prepared manifest retains exact durable custody during the recoverable gap before every destination update
  - the prepared manifest remains unchanged until all task after-states verify
  - a retry accepts only exact before or after bytes and rejects every unknown state
  - the manifest phase change is the transaction commit point
  - receiver receipts publish only after commit and bind the exact committed-manifest digest
  - a retry of the prepared digest repairs missing post-commit receipts without replaying task edits
- request
  - schema: `omo-queue-transfer-request/v1`
  - source fields: task, task SHA-256, ordered queue, ordered queue SHA-256
  - destination fields: each existing task and its SHA-256
  - dispositions: one row per zero-based source index in source order
    - `transfer`: exact destination and empty evidence
    - `completed`, `cancelled`, or `duplicate`: empty destination and nonempty one-line evidence
    - repeated source text may transfer once; disposition each extra copy as a terminal duplicate
- commands
  - create an owner-private JSON request and calculate its SHA-256
  - use `.omo-queue-transfer-REQUEST_SHA256.json` as the manifest name
  - prepare
    - `omo_queue_transfer.py prepare --root ROOT --request REQUEST --request-sha256 REQUEST_SHA256 --manifest ROOT/.omo-queue-transfer-REQUEST_SHA256.json`
  - apply the exact prepared-manifest digest printed by `prepare`
    - `omo_queue_transfer.py apply --root ROOT --manifest MANIFEST --manifest-sha256 PREPARED_MANIFEST_SHA256`
  - verify the committed-manifest digest printed by `apply`
    - `omo_queue_transfer.py verify --root ROOT --manifest MANIFEST --manifest-sha256 COMMITTED_MANIFEST_SHA256`
- evidence limits
  - a committed manifest proves the exact source snapshot, every disposition, destination before/after queues, counts, hashes, and receipts
  - unrelated historical count claims cannot be converted into a manifest after the transfer
  - conflicting historical `90`, `116`, and claimed `92` boundaries remain unresolved without an immutable source snapshot and transaction receipts from that event
