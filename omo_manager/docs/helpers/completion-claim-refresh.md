# completion claim refresh

(authored by agents unless marked 🧑)

purpose

- recover one owner-authenticated completion after task bytes change
  - only when the earlier capability provably never crossed the SMTP boundary

contract

- pass the exact earlier claim key with `omo_completion_email.py --refresh-unattempted-claim`
- pass the unchanged semantic key and current completion content
  - a changed task-close claim may use a replacement semantic key
    - only when its fixed subject and body match exactly
- the helper replaces the ledger claim atomically
- the helper rejects used, delivered, reconciled, queued, ambiguous, cross-task, or cross-owner state
  - checks both the earlier and replacement identities
- the earlier authorization remains as inert evidence
