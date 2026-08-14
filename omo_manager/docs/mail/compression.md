# manager mail cleanup and compression

Manager-mail compression does not require an evidence directory or persisted evidence artifacts. Select explicit source messages from a current read-only mailbox view, independently review the proposed task grouping, send and verify one self-contained replacement per task when needed, then move only superseded sources to recoverable Gmail Trash. Never expunge or permanently delete.

A threshold may start review; it never authorizes Trash. PB news, PB stock watch, and PB urgent mail remain excluded.

## select current sources

- start from a current read-only view of configured agent-to-human and legacy self-addressed manager mail in `INBOX`; a human must explicitly authorize other directions or mutation sources
- explicitly select the source messages for the run; later arrivals and unselected messages remain outside the run
- inspect complete thread context and authoritative task/TODO state for every selected source before proposing any grouping or disposition
- keep mailbox bodies and identifiers private and use `OMO_HUMAN_EMAIL_CONFIG_PATH`
- preserve each source task's original leading tmux subject target; `inspect-explicit` reports it as `selected_source_sender_tmux_target=` and `context_sender_tmux_target=`

## group and independently review

- group selected sources by the actual task they concern, not by a generic filename, path list, subject similarity, Gmail label, or thread alone
- have a reviewer distinct from the preparer independently review the proposed task/source grouping and every proposed disposition against a current read-only mailbox view
- resolve disagreements without expanding the selected source set; if the grouping, task state, or safe disposition remains uncertain, stop and ask a focused human question
- every task must end with exactly one useful, self-contained manager email; consolidate all distinct useful context into that message rather than leaving multiple partial messages
- Gmail state signals—including unread, Important, starred, flagged, saved, read-later, categories, and all other flags or labels—are audit metadata, never retention or Trash criteria

## replace, verify, then trash

- run `inspect-explicit --task-id TASK --uids '12,34'` on each proposed task group; its `source_uidvalidity=`, `source=`, and deduplicated `context=` values bind the matching `trash-explicit` arguments
- give the inspection output and proposed replacement to a distinct reviewer; pass the preparer and reviewer identities to `trash-explicit`, which rejects identical identities
- after sending a replacement, run `locate-replacement --subject EXACT_SUBJECT` and use its unique `message_id=` value as `trash-explicit --replacement-message-id`
- after all replacements are delivered, rerun `inspect-explicit` for the unchanged source UID set and have the distinct reviewer confirm any changed thread context before using the fresh `context=` bindings
- when one selected source contains multiple tasks, send and independently review one replacement per task, then repeat aligned `--task-id` and `--replacement-message-id` arguments and bind each 1-based task position to every source it covers with `--task-source TASK-INDEX:GMAIL-MSGID`; pass `--source-uidvalidity` from inspection so every replacement and shared source is revalidated before the move
- finish one task at a time
- when one selected source already provides the one useful, self-contained manager message for a task, retain it and do not send a replacement
- when a replacement is needed, send exactly one self-contained replacement for the task and verify that exact message is uniquely present in the recipient mailbox before any source mutation; if delivery verification is delayed, look up the same message identity rather than sending a duplicate
- send each replacement with `email_me.py --sender-tmux-target ORIGINAL_TARGET` plus `--subject-file` and `--message-file` as needed; this reuses the source task's tmux subject tag and thread instead of the compression worker's target
- require the independently reviewed task grouping to identify one original target from its selected sources and current thread context; if the target is missing or conflicting, stop and resolve it without defaulting to the compression worker's target
- `trash-explicit` independently derives that target from the live-bound sources and context, fetches each verified replacement subject, and blocks Trash unless every replacement preserves its task's target through the final mutation gate
- immediately before mutation, use a current read-only mailbox view to recheck the selected source identities, task grouping, disposition, and replacement; changed, missing, duplicate, or ambiguous mail blocks that task without affecting reviewed tasks
- move only the explicitly selected, independently reviewed sources that the retained message or verified replacement supersedes, and move them only to recoverable `[Gmail]/Trash`
- never expunge, permanently delete, or mutate `\All`

## finish

- use a current read-only mailbox view to verify that each completed task has exactly one useful, self-contained manager email and that only its superseded selected sources were moved to recoverable Trash
- report concisely: topics, selected and trashed counts, retained or replacement messages, unresolved exceptions, verification status, and zero permanent deletions; omit private bodies and identifiers
