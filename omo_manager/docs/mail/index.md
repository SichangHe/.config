# manager mail

- `accounts.md`
  - separate agent communication mail from human mailbox cleanup
- `cleanup.md`
  - shared classification and execution safeguards for manager mail
  - clean stale manager-human threads conservatively and recoverably
- `compression.md`
  - compress only fully superseded manager-sent mail; Gmail read/unread state never determines eligibility
  - inherit cleanup safeguards and move sources only after replacement
- `email-idle-watcher.md`
  - ingest human email into durable manager pending blocks
- `digest-and-email.md`
  - durable digest queue and manager-human email sending behavior
