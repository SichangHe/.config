# manager mail compression

Use this when unread manager-sent email should be replaced by fewer topic emails.

Helper:
- `~/.config/omo_manager/omo_manager_mail_compress.py`

Workflow:
- inspect current instructions and source task
- run `omo_manager_mail_compress.py snapshot`
- create a fresh private export directory, for example `mktemp -d /tmp/manager-mail-compress.XXXXXX`
- run `omo_manager_mail_compress.py export --out-dir PRIVATE_DIR`
- group topics from `manifest.tsv`, `uids.txt`, and `UID.txt`
- retain any memo the human should read in full
- write `superseded-uids.txt` with only UIDs fully replaced by new summaries
- send summaries with `email_me.py --manager-human --sender-tmux-target OWNER_TARGET --subject-file SUBJECT --message-file BODY`
- run `omo_manager_mail_compress.py trash-superseded --uid-file PRIVATE_DIR/superseded-uids.txt --yes`
- verify `verify_remaining=0`
- report topics, counts, UID boundary, replacement subjects, skipped boundary, and verification
- delete the private export directory when raw local copies are no longer needed

Safety:
- keep private bodies in `/tmp` or another owner-only scratch directory
- export refuses a non-empty output directory
- snapshot/export use the human mailbox config and search only unread mail from the configured agent address to the configured human address; legacy self-addressed cleanup remains restricted to historical `[a]` or `[omo_manager]` mail
- the helper re-parses headers and skips boundary mismatches before exporting bodies
- `trash-superseded` acts only on the explicit UID list
- before moving mail, `trash-superseded` rechecks each UID still matches that sender/recipient boundary in `INBOX`
- the helper moves only explicit superseded source mail to `[Gmail]/Trash`
- it never expunges or permanently deletes message bodies

Judgment:
- the helper does not choose topics or summarize bodies
- the agent decides what remains useful to the human
- retain full-read memo emails instead of replacing them
- omit low-level implementation trivia unless the human needs it
