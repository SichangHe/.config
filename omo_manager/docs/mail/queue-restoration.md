# authenticated mailbox queue restoration

(authored by agents unless marked 🧑)

- scope
  - `omo_mail_queue_restore.py` repairs only `mail_cleanup_w.md`
  - no mailbox, read-state, PB-state, task lifecycle, or pane action
- evidence
  - authenticated Git commit and blob digest identify the 47-item source
  - source order and exact IDs identify the two reconciled newest items
  - the remaining 45 source lines precede the exact current 64- and 65-message items
- transaction
  - canonical Git root, path type, ancestry, task identity, queue sets, and counts must match
  - target and file locks serialize every supported task writer through the fresh current-byte digest check and atomic replacement
  - the exact-CAS guarantee applies to supported writers that use the shared task locks
  - unsupported direct writes during the final operating-system replacement window cannot be detected
  - all bytes after the current queue remain exact
- execution
  - pass a freshly computed complete current-task SHA-256
  - one successful invocation prints the resulting digest and item counts
