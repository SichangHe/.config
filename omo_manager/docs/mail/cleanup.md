# manager-human mail cleanup

Use [compression.md](compression.md) for every manager-human cleanup run. It defines the immutable fixed-start set, disjoint review batches, independent cross-review, explicit-UID execution, recoverable Trash receipts, and terminal verification. Do not substitute rolling boundaries, repeated scans, Gmail labels, or a big-bang mailbox mutation.

## scope and authority

- default to configured agent-to-human and legacy self-addressed manager mail in `INBOX`; a human must explicitly authorize other directions or mutation sources
- discover `\Sent` and `\All` mailbox identities before the single export, but use them only as read-only context unless the human explicitly names them as mutation sources
- preserve privacy: keep bodies, identifiers, task evidence, reasons, intents, and outcomes in the owner-only evidence directory

## classification

- thresholds and body length trigger review only; neither makes mail eligible for Trash
- read each candidate's complete exported context newest-to-oldest and bind the decision to the authoritative multi-word `TODO.md` heading and current task state
- completed, resolved, or `previous` evidence overrides stale active-looking status; a generic filename or path list is not task evidence
- retain unresolved human decisions, pending or still-relevant work, distinct useful context, out-of-scope context, and genuine uncertainty
- ask a focused human question only when a material disposition remains uncertain
- ignore every Gmail signal, including unread and Important, when deciding retention or Trash

## non-negotiable safeguards

- send and verify any replacement before Trash
- move only independently reviewed fixed-start UIDs to `[Gmail]/Trash`; never expunge or permanently delete
- fail closed and isolate only the affected thread on identity, content, task, replacement, mailbox-location, or frozen-member drift; additive later identities remain outside the run
- preserve immutable receipts and source evidence until interrupted work is reconciled and the full manifest verifies
