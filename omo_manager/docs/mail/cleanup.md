# manager-human mail cleanup

Use this when stale manager-human email threads should be moved out of `INBOX`.
This workflow is reversible cleanup: move mail to `[Gmail]/Trash`, never expunge.

Scope:
- manager-human `[a]` mail
- old `[omo_manager]` mail for compatibility
- `INBOX` only

Classify read-only:
- open `INBOX` with the config used by `email_idle_watcher.py`
- search self-addressed manager mail in scope
- fetch only flags, size, thread id, date, and subject
- group by Gmail thread id
- decide at thread level

Retain every thread with:
- any unread message
- recent context
- link to `TODO.md` `current` or `human pending`
- link to unresolved `(pending)` email refs in current `work_manager_*.md`
- long report/update message
- uncertain routing, active worker relevance, or human-pending relevance

Trigger:
- `email_idle_watcher.py` queues this workflow when recent manager-human mail within `24` hours is more than `64`
- the threshold is only a trigger
- it is not deletion eligibility

Move:
- rerun the same classification in a read-write `INBOX` session immediately before moving
- verify `[Gmail]/Trash` exists
- abort if any selected UID lacks `\Seen`
- move only selected explicit UIDs with IMAP `UID MOVE ... "[Gmail]/Trash"`
- verify selected UIDs no longer remain in `INBOX`

Report:
- initial scoped message/thread counts
- moved message/thread counts
- retained message/thread counts
- retain-reason counts
- verification that moved UIDs left `INBOX`
- examples or classes intentionally retained

Safety:
- cleanup uses explicit IMAP UIDs because watcher source files are named by UID
- use `~/.config/omo_manager/omo_manager_mail_compress.py` only for unread manager-sent mail compression, not stale-thread cleanup
- use file-based email bodies for replacement text, for example `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`
- avoid permanent deletion and all-mail operations
- retain when classification depends on judgment
