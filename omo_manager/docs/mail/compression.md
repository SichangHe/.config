# manager mail compression

Use this only to replace unread manager-sent mail with fewer topic summaries.
Follow [cleanup.md](cleanup.md)'s task-authority, evidence, recovery, and mutation safeguards. Compression uses the fixed-start source set below instead of rolling cleanup discovery. A threshold starts review; it never authorizes compression or Trash.

Helper:

- `~/.config/omo_manager/omo_manager_mail_compress.py`

## freeze once

- create a fresh owner-only directory, then run `export --out-dir PRIVATE_DIR --threads-per-batch N`
- that export performs the run's only candidate discovery and freezes the accepted UIDs, Gmail message and thread identities, content and thread digests, labels, UIDVALIDITY, and deterministic batches
- `snapshot` and `identity-preflight` are optional diagnostics before the run; they never define or extend its source set
- later arrivals are outside the run, never enter a batch, and never trigger another discovery pass
- missing, duplicate, conflicting, or incomplete Gmail identity or complete-thread evidence blocks export without mutation
- use `OMO_HUMAN_EMAIL_CONFIG_PATH`; no Gmail REST API, OAuth token, browser profile, or application-default credential is required

## review bounded batches

- claim one batch with `claim-batch --source-dir PRIVATE_DIR --batch-id BATCH --owner OWNER`
- claims are exclusive; reviewers may process different batches in parallel, but no thread may have duplicate ownership or move across batches
- inspect each thread's exported bodies and complete `\All` context, then locate corresponding task records and record current task-state evidence
- retain unresolved, pending, useful, protected, out-of-scope, incomplete, or uncertain content
- Gmail Important alone is never a retention gate; flagged, starred, saved, and read-later intent still requires retention
- finish one thread before advancing: retain the whole thread, move every irrelevant fixed-start source in it, or move only irrelevant intermediate fixed-start messages
- record retention with `retain-thread` and nonempty reason and task-evidence files
- when replacement is required, send and record it before Trash and pass `--replacement-message-id`; otherwise pass `--replacement-not-required`
- list only the chosen thread's source UIDs and run `trash-superseded` with its source directory, claimed batch, owner, Gmail thread ID, reason file, task-evidence file, replacement decision, and `--yes`
- the helper writes an immutable intent before mutation, rejects cross-batch or repeated disposition, revalidates source and complete-thread identity/content immediately before `UID MOVE`, and writes an outcome only after immediate Trash verification
- if an interrupted Trash intent is mixed across INBOX and Trash, `trash-superseded` verifies and skips each exact Trash source, revalidates the combined complete thread, and moves only the still-intact INBOX remainder; it never retries an already-Trashed source
- otherwise, if a caller interruption leaves an intent without an outcome and every source may already match its final disposition, stop mutation retries and run `reconcile-intent --source-dir PRIVATE_DIR --gmail-thread-id THREAD`; it opens IMAP read-only, requires every frozen source in exactly its intended INBOX or `[Gmail]/Trash` location, rechecks exact identity/content and the complete thread digest, then writes only the missing local outcome receipt
- reconciliation fails closed if the intent is missing or malformed, an outcome already exists, a source is duplicate, absent, in both or the wrong mailbox, or source/thread evidence changed
- identity, content, label, UIDVALIDITY, task, or thread drift fails closed for that thread without broadening or rerunning the source set

## finish

- run `verify-run --source-dir PRIVATE_DIR`
- final verification reconciles `run.tsv`, `manifest.tsv`, `batches.tsv`, exclusive claims, and every per-thread outcome
- completion requires every fixed-start source to be classified exactly once as retained or verified in Trash
- an intent without an outcome blocks completion
- final verification performs no full live candidate scan; later arrivals remain outside the run
- report only topics, fixed-start counts, retained and trashed counts, replacement identities, drift, and verification status

## safety

- keep exported bodies, task evidence, identities, intents, and outcomes private
- use only explicit fixed-start UIDs from one claimed thread per mutation
- move mail only to `[Gmail]/Trash`; never expunge or permanently delete it
- immediate Trash verification uses frozen Gmail message identities, so unrelated later arrivals do not alter termination
- preserve source mailbox and label evidence needed for recovery
- PB news, PB stock watch, and PB urgent mail remains excluded
- delete private evidence only when recovery is no longer needed
