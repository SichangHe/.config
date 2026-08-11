# manager mail compression

Use this only to replace manager-sent mail with fewer topic summaries.
Follow [cleanup.md](cleanup.md)'s task-authority, evidence, recovery, and mutation safeguards. Compression uses the fixed-start source set below instead of rolling cleanup discovery. A threshold starts review; it never authorizes compression or Trash.

Helper:

- `~/.config/omo_manager/omo_manager_mail_compress.py`

## freeze once

- create a fresh owner-only directory, then run `export --out-dir PRIVATE_DIR --threads-per-batch N`
- that export performs the run's only candidate discovery and freezes the accepted UIDs, Gmail message and thread identities, content, UIDVALIDITY, and deterministic batches; recorded signal metadata is audit-only and never a gate
- `snapshot` and `identity-preflight` are optional diagnostics before the run; they never define or extend its source set
- later arrivals are outside the run, never enter a batch, and never trigger another discovery pass
- missing, duplicate, conflicting, or incomplete Gmail identity or complete-thread evidence blocks export without mutation
- use `OMO_HUMAN_EMAIL_CONFIG_PATH`; no Gmail REST API, OAuth token, browser profile, or application-default credential is required

## review bounded batches

- claim one batch with `claim-batch --source-dir PRIVATE_DIR --batch-id BATCH --owner OWNER`
- claims are exclusive; reviewers may process different batches in parallel, but no thread may have duplicate ownership or move across batches
- inspect each thread's exported bodies and complete `\All` context, then locate corresponding task records and record current task-state evidence
- retain unresolved, pending, useful, out-of-scope, incomplete, or uncertain content based on message/task evidence
- ignore Gmail state signals—including Seen/unread, Important, starred, flagged, saved, read-later, security/category, and all other flags/labels—through discovery, retention, mutation, reconciliation, recovery, and verification; signal-only drift never blocks progress or changes disposition
- finish one thread before advancing: retain the whole thread, move every irrelevant fixed-start source in it, or move only irrelevant intermediate fixed-start messages
- record retention with `retain-thread` and nonempty reason and task-evidence files
- when replacement is required, send and record it before Trash and pass `--replacement-message-id`; the helper verifies its exact Message-ID and agent-to-human direction in the recipient account's All Mail, which also supports split sender/recipient accounts; if delivery is not yet searchable, retry verification with that same Message-ID and never send a duplicate replacement; otherwise pass `--replacement-not-required`
- list only the chosen thread's source UIDs and run `trash-superseded` with its source directory, claimed batch, owner, Gmail thread ID, reason file, task-evidence file, replacement decision, and `--yes`
- the helper cryptographically binds the complete frozen thread snapshot, requires every frozen identity/content member unchanged, permits only additive later identities, and never adds those later identities to the explicit move set
- the helper writes an immutable intent before mutation, rejects cross-batch or repeated disposition, revalidates frozen source/context identity and content immediately before `UID MOVE`, and writes an outcome only after immediate Trash verification plus a re-fetch of the frozen subset and additive-only relation
- if an interrupted Trash intent is mixed across INBOX and Trash, `trash-superseded` verifies and skips each exact Trash source, revalidates the combined complete thread, and moves only the still-intact INBOX remainder; it never retries an already-Trashed source
- otherwise, if a caller interruption leaves an intent without an outcome and every source may already match its final disposition, stop mutation retries and run `reconcile-intent --source-dir PRIVATE_DIR --gmail-thread-id THREAD`; it opens IMAP read-only, requires every frozen source in exactly its intended INBOX or `[Gmail]/Trash` location, rechecks exact identity/content and the complete thread digest, then writes only the missing local outcome receipt
- normal `reconcile-intent` also tolerates additive later identities; reserve `recover-already-trashed` for an explicitly audited terminal classification of genuinely unchanged-but-unmovable all-Trash frozen sources, never as a shortcut around a failed frozen-member gate
- reconciliation fails closed if the intent is missing or malformed, an outcome already exists, a source is duplicate, absent, in both or the wrong mailbox, or source/thread evidence changed
- identity, content, UIDVALIDITY, task, physical mailbox location, or thread-membership drift fails closed for that thread without broadening or rerunning the source set

## finish

- run `verify-run --source-dir PRIVATE_DIR`
- final verification reconciles `run.tsv`, `manifest.tsv`, `batches.tsv`, exclusive claims, and every per-thread outcome, and reports all threads lacking terminal evidence together
- completion requires every fixed-start source to be classified exactly once as retained or verified in Trash
- an intent without an outcome blocks completion unless it has a valid explicit `skipped_already_trashed` terminal recovery receipt; `verify-run` reports those sources and threads separately from normal Trash outcomes
- final verification performs no full live candidate scan; later arrivals remain outside the run
- report only topics, fixed-start counts, retained and trashed counts, replacement identities, drift, and verification status

## safety

- keep exported bodies, task evidence, identities, intents, and outcomes private
- use only explicit fixed-start UIDs from one claimed thread per mutation
- move mail only to `[Gmail]/Trash`; never expunge or permanently delete it
- immediate Trash verification uses frozen Gmail message identities, so unrelated later arrivals do not alter termination
- preserve source mailbox evidence needed for recoverable Trash handling; never restore or act on Gmail signal metadata
- PB news, PB stock watch, and PB urgent mail remains excluded
- delete private evidence only when recovery is no longer needed
