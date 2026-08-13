# manager mail cleanup and compression

Use this procedure for manager-human mailbox cleanup, including replacement with topic summaries. A threshold may start review; it never authorizes Trash.

Helper: `~/.config/omo_manager/omo_manager_mail_compress.py`

## freeze once

- create a fresh owner-only directory and run `export --out-dir PRIVATE_DIR --threads-per-batch N`
- a corrected successor must add `--scope-file REVIEWED.tsv`; the owner-only regular-file v1.0.0 TSV columns are `version`, `task_id`, `uid`, `gmail_msgid`, `gmail_thrid`, `raw_sha256`, `preparer`, `reviewer`, and `provenance`. Every row repeats the same nonempty review metadata, and reviewer must differ from preparer
- build that scope only from a fresh read-only view of currently present mail and have a distinct reviewer approve the exact task/source mapping. Prior-run evidence may inform provenance but is evidence only, never mutation authority
- scoped export takes a new fixed-start snapshot, excludes unrelated and later-arriving candidates, rejects missing, duplicate, boundary-mismatched, task-conflicting, or content/identity-drifted sources, and binds the scope-file SHA-256 plus review identities in immutable `scope.tsv`
- scoped export still requires an explicitly authorized fresh private output directory; the helper does not grant directory-creation authority
- export is the run's only candidate discovery; it freezes one immutable fixed-start source set, exact message and thread identities, content, complete `\All` thread context, UIDVALIDITY, and deterministic disjoint thread batches
- later arrivals are outside the run: never classify or move them, never add them to a batch, and never rerun discovery because of them
- missing, duplicate, conflicting, or incomplete identity or context evidence blocks export without mutation
- every IMAP operation has an absolute deadline; timeout diagnostics name the failed stage, abort the connection, do not retry a timed-out fetch, and leave no manifest
- export always fsyncs one owner-private hidden terminal receipt beside the attempted run directory before returning or raising; it binds the directory by path digest, records a sanitized exact exit category and stage, contains no mail or proof content, grants no run authority, and blocks same-directory discovery retry
- only a complete `manifest.tsv` inside the run directory makes the fixed-start set authoritative; an empty or partial directory and its sibling terminal receipt do not
- keep exported bodies, identities, and evidence private; use `OMO_HUMAN_EMAIL_CONFIG_PATH`

## classify and cross-review

- claim batches exclusively with `claim-batch`; different owners may classify disjoint batches in parallel
- inspect every exported thread with its complete context and authoritative task/TODO evidence; record the exact task state, reason, and proposed disposition
- retain unresolved, pending, useful, or out-of-scope content only while the run remains unfinished
- keep at most one useful manager email per task during reconciliation, and every task must ultimately have exactly one self-contained useful manager email. Consolidate all distinct useful context into one exact verified replacement before moving superseded fixed-start originals to recoverable Trash
- uncertainty, insufficient context, tooling limits, or an unsafe consolidation are blockers requiring repair and independent review. They never authorize forced mutation, terminal multi-retain, or a permanent `cannot safely consolidate` exception
- Gmail state signals—including unread, Important, starred, flagged, saved, read-later, categories, and all other flags or labels—are audit metadata, never retention or Trash criteria
- require a reviewer distinct from the batch owner to cross-review each proposed disposition against the same frozen evidence before execution; pass that identity with `--reviewer` so owner and reviewer are machine-checked and the digest is bound into the immutable intent and outcome, record the approval in the task-evidence file, and resolve disagreements without expanding the source set
- finish one thread at a time: retain it, move all irrelevant fixed-start sources, or move only irrelevant intermediate fixed-start sources

## execute reviewed UIDs

- bind every disposition to the same explicit `--task-id` for that task; record a sole retention with `retain-thread` and nonempty reason and task-evidence files
- if a useful replacement is required, send and record it before Trash; use its exact Message-ID with `--replacement-message-id`, retry lookup of that same identity if delivery is delayed, and never send a duplicate replacement. The exact syntactically valid identity is bound into immutable intent/outcome evidence and verified uniquely in the recipient mailbox before mutation. Use `--replacement-not-required` only when exactly one fixed-start original will remain retained for the task
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
- completion requires every fixed-start source to have exactly one verified retained or Trash disposition, every thread to have terminal evidence, and every task to end with exactly one useful manager message. Multiple retained threads for one task block verification unless exactly one replacement identity is bound and every superseded fixed-start original has a recoverable terminal Trash disposition
- verification performs no repeated candidate scan or full live mailbox scan; later arrivals neither enter the result nor trigger another run
- report concisely: topics, fixed-start/thread counts, retained and trashed counts, replacement identities, isolated drift or recovery exceptions, verification status, and zero permanent deletions; omit private bodies, identifiers, and task evidence

PB news, PB stock watch, and PB urgent mail remains excluded.
