# manager-human mail cleanup workflow

Use this when the human asks to clean stale email threads between the human and manager. This workflow is for reversible cleanup only: move mail to `[Gmail]/Trash`, never expunge.

Entry point:
- manager instruction: spawn a worker and point it at this doc
- prior task context: `/ssd1/sichangheagent/work_logs/manager_email_cleanup_6169.md`
- helper index: `~/.config/omo_manager/MANAGER_HELPERS.md`

Tools:
- `~/.config/omo_manager/email_idle_watcher.py`
  - reads the Gmail/Himalaya config
  - stores accepted human replies as `manager_mail/UID.txt`
  - marks processed source inbox mail seen
  - records manager mail counts and queues threshold cleanup work
- `~/.config/omo_manager/omo_manager_mail_compress.py`
  - only compresses unread manager-sent mail and moves explicit superseded source UIDs to Trash
  - not a stale-thread trash cleanup helper
- `~/.local/bin/himalaya folder list`
  - verifies `[Gmail]/Trash` exists
- direct IMAP `UID MOVE`
  - cleanup uses explicit IMAP UIDs because watcher source files are named by UID

Read-only classification:
- open `INBOX` read-only with the config from `email_idle_watcher.py`
- search `FROM configured-self-address SUBJECT "[a]"` plus old `[omo_manager]`
- fetch only `FLAGS`, `RFC822.SIZE`, `X-GM-THRID`, and header fields with `BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)]`
- group by `X-GM-THRID`, then decide at thread level

Retain every thread with any of these properties:
- unread message
- recent context
- linked to `TODO.md` `current` or `human pending`
- linked to unresolved `(pending)` email source refs in current `work_manager_*.md`
- long report/update message
- uncertain routing, active worker relevance, or human-pending relevance

Watcher trigger:
- `email_idle_watcher.py` queues this workflow when manager-human `[a]` mail, plus old `[omo_manager]` compatibility, within the last `24` hours is more than `64`
- the `64` threshold is only a trigger scope; it is not deletion eligibility
- compression/replacement text must go through files, for example `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`
- trash movement remains limited to explicit seen stale UIDs after the read-only classification is rerun

Cleanup criteria used in the 2026-06-15 run:
- scoped to `INBOX` self-addressed `[a]` mail plus old `[omo_manager]` mail
- recent cutoff was `2026-06-14 22:00 PDT`
- long-report threshold was `8000` bytes
- active tokens came from `TODO.md` `current` and `human pending`
- `manager_mail/6181.txt` was retained because it was a pending human reply thread

Apply procedure:
- rerun the same classification in a read-write `INBOX` session immediately before moving
- verify `[Gmail]/Trash` appears in the mailbox list
- abort if any selected UID lacks `\Seen`
- move only selected explicit UIDs with IMAP `UID MOVE ... "[Gmail]/Trash"`
- verify `UID SEARCH UID selected-uids` returns zero selected UIDs in `INBOX`

Report:
- initial scoped message/thread counts
- moved message/thread counts
- retained message/thread counts
- retain-reason counts
- verification that moved UIDs no longer remain in `INBOX`
- examples or classes intentionally retained

Safety notes:
- do not use `himalaya message delete`, mailbox purge, expunge, or permanent deletion
- do not operate on all mail, sent mail, or non-manager mail
- prefer retaining a thread when the classification depends on judgment

Human-review note: review and approve this standing cleanup workflow before treating it as policy beyond manager-human `[a]` and old `[omo_manager]` threads.
