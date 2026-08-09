# manager-human mail cleanup

Use this for reversible removal of stale manager-human threads from `INBOX`.
Move mail only to `[Gmail]/Trash`; never expunge or permanently delete it.
Read [compression.md](compression.md) too when a replacement summary is involved.

## scope

Default scope:

- split-account agent-to-human mail from the configured agent address
- legacy self-addressed `[a]` or `[omo_manager]` mail
- `INBOX` only

An authoritative human request may explicitly authorize a bidirectional run.
For any bounded run:

- discover and record the `\Sent` and `\All` special-use mailbox identities before candidate discovery
- freeze exact configured addresses, mutation-candidate mailboxes, inclusive start, and exclusive end before candidate discovery; validate the requested duration and never add later arrivals
- scan `INBOX` by default; use discovered `\Sent` only for an authorized bidirectional run
- use `\Sent` and `\All` as read-only context unless authoritative human text names them as mutation sources; resolve each candidate Gmail thread through `\All` and preserve a thread with any out-of-scope message or time context

## classify

- thresholds and body length trigger review only; neither makes mail eligible for Trash
- open the configured human `INBOX` with `OMO_HUMAN_EMAIL_CONFIG_PATH`; discover exact-boundary metadata, size, date, subject, and Gmail thread ID, then inspect each candidate's complete private context newest-to-oldest before deciding
- record the complete multi-word `TODO.md` heading and authoritative task state; `previous`, completed, or resolved evidence overrides a stale active-looking status
- an active-task link retains only context that is unresolved, pending, or still relevant; a generic filename or path list is not task evidence by itself
- retain `human pending`, live-pending, or unresolved human-decision work unless explicit authoritative closure permits removal; derived reviewer or routing evidence cannot replace that authority
- use a recorded human or reviewer resolution only for derived routing uncertainty or an unknown-file reference; revalidate every known task state before mutation
- ignore Gmail state signals—including unread, Important, starred, flagged, saved, read-later, and security/category labels—when deciding retention or Trash eligibility; retain based only on message/task evidence and uncertainty
- ask focused human questions only when a material disposition is genuinely uncertain; give concrete choices rather than applying a broad fallback

Retain every thread with:

- recent or out-of-scope chain context
- unresolved, pending, or still-relevant task context
- `TODO.md` `human pending`, a live pending item, or an unresolved `(pending)` email reference
- uncertainty about routing, task relevance, or a human decision

## execute

- make a private immutable source map before mutation: mailbox-scoped UID, message and thread identity, source labels, task evidence, reason, and disposition
- rerun the same mailbox, task, pending, and routing classification in a read-write `INBOX` session immediately before mutation; abort on any change and record external drift separately instead of attributing it to cleanup
- create an ordered operation plan, then fsync a paired intent and outcome receipt for every mutation; validate exact target order, identity digest, primary mailbox, and complete source-label list, not only aggregate counts
- send and record a replacement summary before moving any source it fully supersedes
- verify `[Gmail]/Trash` exists; move only explicitly planned UIDs with `UID MOVE ... "[Gmail]/Trash"`; never mutate `\All`
- retain recovery evidence sufficient to restore each moved message to its recorded primary mailbox and labels
- finish with live verification: every selected UID left its source and reached Trash, every retained message remains in source, complete thread membership is unchanged, counts reconcile, and permanent deletions remain zero

## report

Report only high-level scoped, moved, retained, retain-reason, and intentionally retained-class counts; record that final live verification and recovery receipts exist. Keep message bodies, identifiers, and task evidence private.

Safety:

- use explicit IMAP UIDs because watcher source files are UID-named
- use `~/.config/omo_manager/omo_manager_mail_compress.py` only for manager-sent mail compression, not stale-thread cleanup
- use file-based replacement mail, for example `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`
- retain whenever a classification depends on judgment
