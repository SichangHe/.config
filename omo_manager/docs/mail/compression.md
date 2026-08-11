# manager mail cleanup and compression

Use this procedure for manager-human mailbox cleanup, including replacement with topic summaries. A threshold may start review; it never authorizes Trash.

Helper: `~/.config/omo_manager/omo_manager_mail_compress.py`

## freeze once

- create a fresh owner-only directory and run `export --out-dir PRIVATE_DIR --threads-per-batch N`
- export is the run's only candidate discovery; it freezes one immutable fixed-start source set, exact message and thread identities, content, complete `\All` thread context, UIDVALIDITY, and deterministic disjoint thread batches
- later arrivals are outside the run: never classify or move them, never add them to a batch, and never rerun discovery because of them
- missing, duplicate, conflicting, or incomplete identity or context evidence blocks export without mutation
- keep exported bodies, identities, and evidence private; use `OMO_HUMAN_EMAIL_CONFIG_PATH`

## classify and cross-review

- claim batches exclusively with `claim-batch`; different owners may classify disjoint batches in parallel
- inspect every exported thread with its complete context and authoritative task/TODO evidence; record the exact task state, reason, and proposed disposition
- retain unresolved, pending, useful, out-of-scope, incomplete, or uncertain content
- consolidate all distinct useful context into one verified replacement and keep at most one useful manager email per task; if safe consolidation is impossible, isolate the thread rather than discard useful context
- Gmail state signals—including unread, Important, starred, flagged, saved, read-later, categories, and all other flags or labels—are audit metadata, never retention or Trash criteria
- require a reviewer distinct from the batch owner to cross-review each proposed disposition against the same frozen evidence before execution; record that approval in the task-evidence file so its digest is bound into the immutable intent and outcome, and resolve disagreements without expanding the source set
- finish one thread at a time: retain it, move all irrelevant fixed-start sources, or move only irrelevant intermediate fixed-start sources

## execute reviewed UIDs

- record retention with `retain-thread` and nonempty reason and task-evidence files
- if a useful replacement is required, send and record it before Trash; use its exact Message-ID with `--replacement-message-id`, retry lookup of that same identity if delivery is delayed, and never send a duplicate replacement; otherwise use `--replacement-not-required`
- pass only the reviewed fixed-start UIDs from one claimed thread in the private UID file to `trash-superseded`; later identities are never eligible for that file
- immediately before `UID MOVE`, revalidate frozen source identity/content, complete frozen context, task evidence, batch ownership, and replacement evidence
- additive later thread identities are allowed but never moved; changed or missing frozen members, task drift, identity/content drift, or non-additive context drift fails closed for that thread without affecting other batches
- move only to `[Gmail]/Trash`; never expunge, permanently delete, or mutate `\All`
- write and fsync an immutable intent before mutation and an immutable outcome only after exact Trash verification and post-move revalidation

## interruption and recovery

- receipts make execution resumable: never retry a source already verified in Trash and never replace an existing intent with different evidence
- resume an interrupted partial move only by rerunning `trash-superseded` with the identical intent inputs; it verifies and skips exact sources already in Trash, revalidates the combined thread, and moves only the intact `INBOX` remainder
- when every source may already be at its intended final location but the outcome is missing, stop mutation retries and use `reconcile-intent`; this read-only path must prove every frozen source is in exactly its intended `INBOX` or Trash location and revalidate exact frozen evidence before writing only the missing receipt
- ambiguous, absent, duplicated, wrong-mailbox, changed, or malformed evidence remains isolated and unresolved; retain recovery evidence until reconciliation is complete

## finish once

- run `verify-run --source-dir PRIVATE_DIR` against the complete frozen manifest
- completion requires every fixed-start source to have exactly one verified retained or Trash disposition, every thread to have terminal evidence, counts to reconcile, and permanent deletions to remain zero
- verification performs no repeated candidate scan or full live mailbox scan; later arrivals neither enter the result nor trigger another run
- report concisely: topics, fixed-start/thread counts, retained and trashed counts, replacement identities, isolated drift or recovery exceptions, verification status, and zero permanent deletions; omit private bodies, identifiers, and task evidence

PB news, PB stock watch, and PB urgent mail remains excluded.
