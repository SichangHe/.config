# manager-human mail cleanup

Use [compression.md](compression.md) for every manager-human cleanup run. It defines explicit selection from a current read-only mailbox view, independent review of task grouping, exact replacement and verification, recoverable Gmail Trash, and the terminal one-self-contained-message-per-task invariant.

Manager-mail cleanup does not require an evidence directory or persisted evidence artifacts.

## scope and authority

- default to configured agent-to-human and legacy self-addressed manager mail in `INBOX`; a human must explicitly authorize other directions or mutation sources
- use `\Sent` only as read-only context unless the human explicitly names it as a mutation source; always use `\All` as read-only context and never mutate it
- preserve privacy: do not expose mailbox bodies or identifiers in reports

## classification

- thresholds and body length trigger review only; neither makes mail eligible for Trash
- read each selected source's complete current context, including its reported date and From/To direction, and bind the decision to the authoritative multi-word `TODO.md` heading and current task state
- completed, resolved, or `previous` evidence overrides stale active-looking status; a generic filename or path list is not task evidence
- independently review the proposed task/source grouping and disposition
- preserve unresolved human decisions and useful context while blocked, but do not treat multiple retained messages as a terminal outcome; every task must ultimately have exactly one useful, self-contained manager email
- ask a focused human question only when a material grouping or disposition remains uncertain
- ignore every Gmail signal, including unread and Important, when deciding retention or Trash

## non-negotiable safeguards

- send and verify exactly one self-contained replacement per task when needed before Trash
- move only explicitly selected, independently reviewed, superseded sources to recoverable `[Gmail]/Trash`
- recheck source identity, task grouping, and replacement in a current read-only mailbox view immediately before mutation; isolate the affected task on drift or ambiguity
- never expunge or permanently delete
